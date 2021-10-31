"""
Module for torch calibration models and relevant functions
"""
import torch
import numpy as np

from . import utils, linalg, dataset


class JonesModel(utils.Module):
    """
    A generic, antenna-based, direction-independent
    Jones term, relating the model (m) visibility to the
    data (d) visibility for antennas p and q
    and polarizations e and n.
    The Jones matrix for antenna p is constructed

    .. math::

        J_p = \\left[\\begin{array}{cc}J_{ee} & J_{en}\\\\
                    J_{ne} & J_{nn}\\end{array}\\right]

    and its application to the model visibility is

    .. math::

        V^d_{pq} = J_p \\cdot V^m_{pq} \\cdot J_q^\\dagger

    For 1-pol mode, :math:`J_p` is of shape (1, 1),
    For 2-pol mode it is diagonal of shape (2, 2),
    and 4-pol mode it is non-diagonal of shape (2, 2),
    where the off-diagonal are the so called "D-terms".
    """
    def __init__(self, params, ants, bls=None, refant=None, R=None, parameter=True,
                 polmode='1pol', single_ant=False, name=None, vis_type='com'):
        """
        Antenna-based Jones model.

        Parameters
        ----------
        params : tensor
            A tensor of the Jones parameters
            of shape (Npol, Npol, Nantenna, Ntimes, Nfreqs),
            where Nfreqs and Ntimes can be replaced by
            freq_Ncoeff and time_Ncoeff for sparse parameterizations.
        ants : list
            List of antenna numbers associated with an ArrayModel object
            with matched ordering to params' antenna axis, with the
            exception of single_ant mode.
        bls : list
            List of ant-pair tuples that hold the baselines of the
            input visibilities, matched ordering to baseline ax of V
        refant : int, optional
            Reference antenna number from ants list for fixing the gain
            phase. Only needed if JonesResponse gain_type is
            'com', 'phs', or 'dly'.
        R : callable, optional
            An arbitrary response function for the Jones parameters.
            This is a function that takes the params tensor and maps it
            into a (generally) higher dimensional space that can then
            be applied to the model visibilities. See JonesResponse()
        parameter : bool, optional
            If True, treat params as a parameter to be fitted,
            otherwise treat it as fixed to its input value.
        polmode : str, ['1pol', '2pol', '4pol'], optional
            Polarization mode. params must conform to polmode.
            1pol : single linear polarization (default)
            2pol : two linear polarizations (diag of Jones Mat)
            4pol : four linear and cross pol (2x2 Jones Mat)
        single_ant : bool, optional
            If True, solve for a single gain for all antennas.
            Nant of params must be one, but ants can still be
            the size of the array.
        name : str, optional
            Name for this object, stored as self.name
        vis_type : str, optional
            Type of visibility, complex or delay ['com', 'dly']
        """
        super().__init__(name=name)
        self.params = params
        self.device = params.device
        self.refant, self.refant_idx = refant, None
        self.ants = ants
        if self.refant is not None:
            assert self.refant in ants, "need a valid refant"
            self.refant_idx = ants.index(self.refant)
        if parameter:
            self.params = torch.nn.Parameter(self.params)
        if R is None:
            # default response
            R = JonesResponse()
        self.R = R
        self.polmode = polmode
        self.single_ant = single_ant
        self._setup(bls)
        self.vis_type = vis_type
        # construct _args for str repr
        self._args = dict(refant=refant, polmode=polmode)
        self._args[self.R.__class__.__name__] = getattr(self.R, '_args', None)

    def _setup(self, bls):
        bls = [tuple(bl) for bl in bls]
        self.bls = bls
        if not self.single_ant:
            self._vis2ants = {bl: (self.ants.index(bl[0]), self.ants.index(bl[1])) for bl in bls}
        else:
            # a single antenna for all baselines
            assert self.params.shape[2] == 1, "params must have 1 antenna for single_ant"
            self._vis2ants = {bl: (0, 0) for bl in bls}

    def fix_refant_phs(self):
        """
        Ensure that the reference antenna phase
        is set to zero: operates inplace.
        This only has an effect if the JonesResponse
        gain_type is ['com', 'dly', 'phs'],
        otherwise params is unaffected.
        """
        with torch.no_grad():
            if self.R.gain_type == 'com':
                if torch.is_complex(self.params):
                    # params is represented as a complex tensor
                    phs = torch.angle(self.params[:, :, self.refant_idx:self.refant_idx+1]).clone()
                    self.params /= torch.exp(1j * phs)
                else:
                    # params is represented as a view_real tensor
                    g = self.params[:, :, self.refant_idx:self.refant_idx+1].clone()
                    amp = linalg.abs(g)
                    self.params -= (g - amp)

            elif self.R.gain_type in ['dly', 'phs']:
                self.params -= self.params[:, :, self.refant_idx:self.refant_idx+1].clone()

    def forward(self, vd, undo=False, prior_cache=None):
        """
        Forward pass vd through the Jones model.

        Parameters
        ----------
        vd : VisData
            Holds model visibilities of shape
            (Npol, Npol, Nbl, Ntimes, Nfreqs).
        undo : bool, optional
            If True, invert params and apply to vd. 
        prior_cache : dict, optional
            Cache for storing computed priors

        Returns
        -------
        VisData
            Predicted visibilities, having forwarded
            vd through the Jones parameters.
        """
        # fix reference antenna if needed
        self.fix_refant_phs()

        # setup if needed
        if vd.bls != self.bls:
            self._setup(vd.bls)

        # push vd to self.device
        vd.push(self.device)

        # setup empty VisData for output
        vout = vd.copy()

        # push through reponse function
        jones = self.R(self.params)

        # invert jones if necessary
        if undo:
            invjones = torch.zeros_like(jones)
            for i in range(jones.shape[2]):
                if self.polmode in ['1pol', '2pol']:
                    if self.vis_type == 'com':
                        invjones[:, :, i] = linalg.diag_inv(jones[:, :, i])
                    elif self.vis_type == 'dly':
                        invjones[:, :, i] = -jones[:, :, i]
                else:
                    assert self.vis_type == 'com', 'must have complex vis_type for 4pol mode'
                    invjones[:, :, i] = torch.pinv(jones[:, :, i])
            jones = invjones

        # get time select (in case we are mini-batching over time axis)
        if vd.Ntimes != jones.shape[-2]:
            tselect = np.ravel([np.where(np.isclose(self.R.times, t, atol=1e-4, rtol=1e-10))[0] for t in vd.times]).tolist()
            diff = list(set(np.diff(tselect)))
            if len(diff) == 1:
                tselect = slice(tselect[0], tselect[-1]+diff[0], diff[0])
            jones = jones[..., tselect, :]

        # iterate through visibility and apply Jones terms
        for i, bl in enumerate(self.bls):
            # pick out jones terms
            j1 = jones[:, :, self._vis2ants[bl][0]]
            j2 = jones[:, :, self._vis2ants[bl][1]]

            if self.polmode in ['1pol', '2pol']:
                if self.vis_type == 'com':
                    vout.data[:, :, i] = linalg.diag_matmul(linalg.diag_matmul(j1, j2.conj()), vd.data[:, :, i])
                elif self.vis_type == 'dly':
                    vout.data[:, :, i] = vd.data[:, :, i] + j1 - j2
            else:
                assert self.vis_type == 'com', "must have complex vis_type for 4pol mode"
                vout.data[:, :, i] = torch.einsum("ab...,bc...,dc...->ad...", j1, vd.data[:, :, i], j2.conj())

        # evaluate priors
        self.eval_prior(prior_cache, inp_params=self.params, out_params=jones)

        return vout

    def push(self, device):
        """
        Push params and other attrs to new device
        """
        self.device = device
        self.params = utils.push(self.params, device)
        self.R.push(device)


class JonesResponse:
    """
    A response object for JonesModel

    Allows for polynomial parameterization across time and/or frequency,
    and for a gain type of complex, amplitude, phase, delay, EW & NS delay slope,
    and EW & NS phase slope (the latter two are relevant for redundant calibration) 
    """
    def __init__(self, freq_mode='channel', time_mode='channel', gain_type='amp',
                 vis_type='com', device=None, freqs=None, times=None, **setup_kwargs):
        """
        Parameters
        ----------
        freq_mode : str, optional
            Frequency parameterization, ['channel', 'poly']
        time_mode : str, optional
            Time parameterization, ['channel', 'poly']
        gain_type : str, optional
            Type of gain parameter. One of
            ['com', 'dly', 'amp', 'phs', 'dly_slope', 'phs_slope']
                'com' : complex gains
                'dly' : delay g = exp(2i * pi * freqs * delay)
                'amp' : amplitude g = amp
                'phs' : phase  g = exp(i * phs)
                '*_slope' : spatial gradient across the array
        vis_type : str, optional
            Type of visibility, complex or delay ['com', 'dly']
        device : str, optional
            Device to place class attributes if needed
        freqs : tensor, optional
            Frequency array [Hz], only needed for poly freq_mode
        times : tensor, optional
            Time array [arb. units], only needed for poly time_mode

        Notes
        -----
        Required setup_kwargs (see self._setup for details)
        if freq_mode == 'poly'
            f0 : float
                Anchor frequency [Hz]
            f_Ndeg : int
                Frequency polynomial degree
            freq_poly_basis : str
                Polynomial basis (see utils.gen_poly_A)

        if time_mode == 'poly'
            t0 : float
                Anchor time [arb. units]
            t_Ndeg : int
                Time polynomial degree
            time_poly_basis : str
                Polynomial basis (see utils.gen_poly_A)

        if gain_type == 'phs_slope' or 'dly_slope:
            antpos : dictionary
                Antenna vector in local ENU frame [meters]
                number as key, tensor (x, y, z) as value
            params tensor is assumed to hold the [EW, NS]
            slope along its antenna axis.
        """
        self.freq_mode = freq_mode
        self.time_mode = time_mode
        self.gain_type = gain_type
        self.vis_type = vis_type
        self.device = device
        self.freqs = freqs
        self.times = times
        self.setup_kwargs = setup_kwargs
        self._setup(**setup_kwargs)

    def _setup(self, f0=None, f_Ndeg=None, freq_poly_basis='direct',
               t0=None, t_Ndeg=None, time_poly_basis='direct', antpos=None):
        """
        Setup the JonesResponse given the mode and type

        Parameters
        ----------
        f0 : float
            anchor frequency for poly [Hz]
        f_Ndeg : int
            Number of frquency degrees for poly
        freq_poly_basis : str
            Polynomial basis across freq (see utils.gen_poly_A)
        t0 : float
            anchor time for poly
        t_Ndeg : int
            Number of time degrees for poly
        time_poly_basis : str
            Polynomial basis across time (see utils.gen_poly_A)
        antpos : dict
            Antenna position dictionary for dly_slope or phs_slope
        """
        assert self.gain_type in ['com', 'amp', 'phs', 'dly', 'phs_slope', 'dly_slope']
        if self.freq_mode == 'channel':
            pass  # nothing to do
        elif self.freq_mode == 'poly':
            # get polynomial A matrix wrt freq
            assert f_Ndeg is not None, "need f_Ndeg for poly freq_mode"
            if f0 is None:
                f0 = self.freqs.mean()
            self.dfreqs = (self.freqs - f0) / 1e6  # MHz
            self.freq_A = utils.gen_poly_A(self.dfreqs, f_Ndeg,
                                           basis=freq_poly_basis, device=self.device)

        if self.time_mode == 'channel':
            pass  # nothing to do
        elif self.time_mode == 'poly':
            # get polynomial A matrix wrt times
            assert t_Ndeg is not None, "need t_Ndeg for poly time_mode"
            if t0 is None:
                t0 = self.times.mean()
            self.dtimes = self.times - t0
            self.time_A = utils.gen_poly_A(self.dtimes, t_Ndeg,
                                           basis=time_poly_basis, device=self.device)

        if self.gain_type in ['dly_slope', 'phs_slope']:
            # setup antpos tensors
            assert antpos is not None, 'need antpos for dly_slope or phs_slope'
            self.antpos = antpos
            EW = torch.as_tensor([antpos[a][0] for a in antpos], device=self.device)
            self.antpos_EW = EW[None, None, :, None, None]  
            NS = torch.as_tensor([antpos[a][1] for a in antpos], device=self.device)
            self.antpos_NS = NS[None, None, :, None, None]

        elif 'dly' in self.gain_type:
            assert self.freqs is not None, 'need frequencies for delay gain type'

        # construct _args for str repr
        self._args = dict(freq_mode=self.freq_mode, time_mode=self.time_mode,
                          gain_type=self.gain_type)

    def param2gain(self, jones):
        """
        Convert params to complex gain given gain_type.
        Note this should be after passing params through
        its response function, such that the jones tensor
        is a function of time and frequency.

        Parameters
        ----------
        jones : tensor
            jones parameter of shape (Npol, Npol, Nant, Ntimes, Nfreqs)

        Returns
        -------
        tensor
            Complex gain tensor (Npol, Npol, Nant, Ntimes, Nfreqs)
        """
        # convert to gains
        if self.gain_type == 'com':
            # assume params are complex gains
            return jones

        elif self.gain_type == 'dly':
            # assume params are in delay [nanosec]
            if self.vis_type == 'dly':
                return jones
            elif self.vis_type == 'com':
                return torch.exp(2j * np.pi * jones * torch.as_tensor(self.freqs / 1e9, dtype=jones.dtype))

        elif self.gain_type == 'amp':
            # assume params are gain amplitude: not exp(amp)!
            return jones + 0j

        elif self.gain_type == 'phs':
            return torch.exp(1j * jones)

        elif self.gain_type == 'dly_slope':
            # extract EW and NS delay slopes: ns / meter
            EW = jones[:, :, :1]
            NS = jones[:, :, 1:]
            # get total delay per antenna
            tot_dly = EW * self.antpos_EW \
                      + NS * self.antpos_NS
            if self.vis_type == 'com':
                # convert to complex gains
                return torch.exp(2j * np.pi * tot_dly * self.freqs / 1e9)
            elif self.vis_type == 'dly':
                return tot_dly

        elif self.gain_type == 'phs_slope':
            # extract EW and NS phase slopes: rad / meter
            EW = jones[:, :, :1]
            NS = jones[:, :, 1:]
            # get total phase per antenna
            tot_phs = EW * self.antpos_EW \
                      + NS * self.antpos_NS
            # convert to complex gains
            return torch.exp(1j * tot_phs)

    def forward(self, params):
        """
        Forward pass params through response to get
        complex antenna gains per time and frequency
        """
        # detect if params needs to be casted into complex
        if self.gain_type == 'com' and not torch.is_complex(params):
            params = utils.viewcomp(params)

        # convert representation to full Ntimes, Nfreqs
        if self.freq_mode == 'channel':
            pass
        elif self.freq_mode == 'poly':
            params = params @ self.freq_A.T
        if self.time_mode == 'channel':
            pass
        elif self.time_mode == 'poly':
            params = (params.moveaxis(-2, -1) @ self.time_A.T).moveaxis(-1, -2)

        # convert gain types to complex gains
        jones = self.param2gain(params)

        return jones

    def __call__(self, params):
        return self.forward(params)

    def push(self, device):
        """
        Push class attrs to new device
        """
        self.device = device
        if self.freq_mode == 'poly':
            self.dfreqs = self.dfreqs.to(device)
            self.freq_A = self.freq_A.to(device)
        if self.time_mode == 'poly':
            self.dtimes = self.dtimes.to(device)
            self.time_A = self.time_A.to(device)
        if self.gain_type in ['dly_slope', 'phs_slope']:
            self.antpos_EW = self.antpos_EW.to(device) 
            self.antpos_NS = self.antpos_NS.to(device)


class RedVisModel(utils.Module):
    """
    Redundant visibility model (r) relating the starting
    model visibility (m) to the data visibility (d)
    for antennas j and k.

    .. math::

        V^{d}_{jk} = V^{r} + V^{m}_{jk}

    """
    def __init__(self, params, vis2red, R=None, parameter=True, name=None):
        """
        Redundant visibility model

        Parameters
        ----------
        params : tensor
            Initial redundant visibility tensor
            of shape (Npol, Npol, Nredvis, Ntimes, Nfreqs) where Nredvis
            is the number of unique baseline types.
        vis2red : list of int
            A list of length Nvis--the length of vd input to
            self.forward()--whose elements index self.params Nredvis axis.
        R : VisModelResponse object, optional
            A response function for the redundant visibility
            model parameterization. Default is freq and time channels.
        parameter : bool, optional
            If True, treat params as a parameter to be fitted,
            otherwise treat it as fixed to its input value.
        name : str, optional
            Name for this object, stored as self.name
        """
        super().__init__(name=name)
        self.params = params
        self.device = params.device
        if parameter:
            self.params = torch.nn.Parameter(self.params)
        self.vis2red = vis2red
        if R is None:
            # default response is per freq channel and time bin
            R = VisModelResponse()
        self.R = R

    def forward(self, vd, undo=False, prior_cache=None):
        """
        Forward pass vd through redundant
        model term.

        Parameters
        ----------
        vd : VisData, optional
            Starting model visibilities of shape
            (Npol, Npol, Nbl, Ntimes, Nfreqs). In the general case,
            this should be a unit matrix so that the
            predicted visibilities are simply the redundant
            model. However, if you have a model of per-baseline
            non-redundancies, these could be included by putting
            them into vd.
        undo : bool, optional
            If True, push vd backwards through the model.
        prior_cache : dict, optional
            Cache for holding computed priors.

        Returns
        -------
        VisData
            The predicted visibilities, having pushed vd through
            the redundant visibility model.
        """
        if vd is None:
            vd = dataset.VisData()
        # push to device
        vd.push(self.device)

        # setup predicted visibility
        vout = vd.copy()

        redvis = self.R(self.params)

        # iterate through vis and apply redundant model
        for i in range(vout.data.shape[2]):
            if not undo:
                vout.data[:, :, i] = vd.data[:, :, i] + redvis[:, :, self.vis2red[i]]
            else:
                vout.data[:, :, i] = vd.data[:, :, i] - redvis[:, :, self.vis2red[i]]

        # evaluate priors
        self.eval_prior(prior_cache, inp_params=self.params, out_params=redvis)

        return vout

    def push(self, device):
        """
        Push to a new device
        """
        self.params = utils.push(self.params, device)


class VisModel(utils.Module):
    """
    Visibility model (v) relating the starting
    model visibility (m) to the data visibility (d)
    for antennas j and k.

    .. math::

        V^{d}_{jk} = V^{v}_{jk} + V^{m}_{jk} 

    """
    def __init__(self, params, R=None, parameter=True, name=None):
        """
        Visibility model

        Parameters
        ----------
        params : tensor
            Visibility model parameter of shape
            (Npol, Npol, Nbl, Ntimes, Nfreqs). Ordering should
            match ordering of vd input to self.forward.
        R : callable, optional
            An arbitrary response function for the
            visibility model, mapping the parameters
            to the space of vd (input to self.forward).
            Default (None) is unit response.
            Note this must use torch functions.
        parameter : bool, optional
            If True, treat vis as a parameter to be fitted,
            otherwise treat it as fixed to its input value.
        name : str, optional
            Name for this object, stored as self.name
        """
        super().__init__(name=name)
        self.params = params
        self.device = params.device
        if parameter:
            self.params = torch.nn.Parameter(self.params)
        if R is None:
            # default response is per freq channel and time bin
            R = VisModelResponse()
        self.R = R

    def forward(self, vd, undo=False, prior_cache=None, **kwargs):
        """
        Forward pass vd through visibility
        model term.

        Parameters
        ----------
        vd : VisData
            Starting model visibilities
            of shape (Npol, Npol, Nbl, Ntimes, Nfreqs). In the general case,
            this should be a zero tensor so that the
            predicted visibilities are simply the redundant
            model. However, if you have a model of per-baseline
            non-redundancies, these could be included by putting
            them into vd.
        undo : bool, optional
            If True, push vd backwards through the model.
        prior_cache : dict, optional
            Cache for storing computed priors

        Returns
        -------
        VisData
            The predicted visibilities, having summed vd
            with the visibility model.
        """
        vout = vd.copy()
        vis = self.R(self.params)
        if not undo:
            vout.data = vout.data + vis
        else:
            vout.data = vout.data - vis

        # evaluate priors
        self.eval_prior(prior_cache, inp_params=self.params, out_params=vis)

        return vout

    def push(self, device):
        """
        Push to a new device
        """
        self.params = utils.push(self.params, device)


class VisModelResponse:
    """
    A response object for VisModel and RedVisModel
    """
    def __init__(self, freq_mode='channel', time_mode='channel',
                 freqs=None, times=None, device=None, **setup_kwargs):
        """
        Parameters
        ----------
        freq_mode : str, optional
            Frequency parameterization, ['channel', 'poly']
        time_mode : str, optional
            Time parameterization, ['channel', 'poly']
        device : str, optional
            Device to place class attributes if needed
        freqs : tensor, optional
            Frequency array [Hz], only needed for poly freq_mode
        times : tensor, optional
            Time array [arb. units], only needed for poly time_mode

        Notes
        -----
        Required setup_kwargs (see self._setup for details)
        if freq_mode == 'poly'
            f0 : float
                Anchor frequency [Hz]
            f_Ndeg : int
                Frequency polynomial degree
            freq_poly_basis : str
                Polynomial basis (see utils.gen_poly_A)

        if time_mode == 'poly'
            t0 : float
                Anchor time [arb. units]
            t_Ndeg : int
                Time polynomial degree
            time_poly_basis : str
                Polynomial basis (see utils.gen_poly_A)
        """
        self.freq_mode = freq_mode
        self.time_mode = time_mode
        self.device = device
        self.freqs = freqs
        self.times = times
        self.setup_kwargs = setup_kwargs
        self._setup(**setup_kwargs)

    def _setup(self, f0=None, f_Ndeg=None, freq_poly_basis='direct',
               t0=None, t_Ndeg=None, time_poly_basis='direct'):
        """
        Setup the JonesResponse given the mode and type

        Parameters
        ----------
        f0 : float
            anchor frequency for poly [Hz]
        f_Ndeg : int
            Number of frquency degrees for poly
        freq_poly_basis : str
            Polynomial basis across freq (see utils.gen_poly_A)
        t0 : float
            anchor time for poly
        t_Ndeg : int
            Number of time degrees for poly
        time_poly_basis : str
            Polynomial basis across time (see utils.gen_poly_A)
        """
        if self.freq_mode == 'channel':
            pass  # nothing to do
        elif self.freq_mode == 'poly':
            # get polynomial A matrix wrt freq
            assert f_Ndeg is not None, "need f_Ndeg for poly freq_mode"
            if f0 is None:
                f0 = self.freqs.mean()
            self.dfreqs = (self.freqs - f0) / 1e6  # MHz
            self.freq_A = utils.gen_poly_A(self.dfreqs, f_Ndeg,
                                           basis=freq_poly_basis, device=self.device)

        if self.time_mode == 'channel':
            pass  # nothing to do
        elif self.time_mode == 'poly':
            # get polynomial A matrix wrt times
            assert t_Ndeg is not None, "need t_Ndeg for poly time_mode"
            if t0 is None:
                t0 = self.times.mean()
            self.dtimes = self.times - t0
            self.time_A = utils.gen_poly_A(self.dtimes, t_Ndeg,
                                           basis=time_poly_basis, device=self.device)

        # construct _args for str repr
        self._args = dict(freq_mode=self.freq_mode, time_mode=self.time_mode)

    def forward(self, params):
        """
        Forward pass params through response to get
        complex vis model per time and frequency
        """
        # detect if params needs to be casted into complex
        if not torch.is_complex(params):
            params = utils.viewcomp(params)

        # convert representation to full Ntimes, Nfreqs
        if self.freq_mode == 'channel':
            pass
        elif self.freq_mode == 'poly':
            params = params @ self.freq_A.T
        if self.time_mode == 'channel':
            pass
        elif self.time_mode == 'poly':
            params = (params.moveaxis(-2, -1) @ self.time_A.T).moveaxis(-1, -2)

        return params

    def __call__(self, params):
        return self.forward(params)

    def push(self, device):
        """
        Push class attrs to new device
        """
        self.device = device
        if self.freq_mode == 'poly':
            self.dfreqs = self.dfreqs.to(device)
            self.freq_A = self.freq_A.to(device)
        if self.time_mode == 'poly':
            self.dtimes = self.dtimes.to(device)
            self.time_A = self.time_A.to(device)


class FFT(utils.Module):
    """
    An FFT block
    """
    def __init__(self, dim=0, abs=False, peaknorm=False, N=None, dx=None):
        """
        Parameters
        ----------
        dim : int, optional
            Dimension to take FFT
        abs : bool, optional
            Take abs after FFT
        peaknorm : bool, optional
            Peak normalize after FFT along dim
        """
        super().__init__()
        self.dim = dim
        self.abs = abs
        self.peaknorm = peaknorm
        self.dx = dx if dx is not None else 1.0
        if N is not None:
            freqs = torch.fft.fftshift(torch.fft.fftfreq(N, d=self.dx))
            self.start = freqs[0]
            self.dx = freqs[1] - freqs[0]
        else:
            self.start = 0.0

    def forward(self, inp, **kwargs):
        """
        Take the FFT of the inp and return
        """
        if isinstance(inp, np.ndarray):
            inp = torch.as_tensor(inp)

        elif isinstance(inp, (dataset.VisData, dataset.MapData)):
            out = inp.copy()
            out.data = self.forward(inp.data, **kwargs)
            return out

        inp_fft = torch.fft.fftshift(torch.fft.fft(inp, dim=self.dim), dim=self.dim)

        if self.abs:
            inp_fft = torch.abs(inp_fft)

        if self.peaknorm:
            inp_fft = inp_fft / torch.max(torch.abs(inp_fft), dim=self.dim, keepdim=True).values

        return inp_fft


class PeakDelay(FFT):
    """
    Compute peak delay across dim
    """
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def k(self, x):
        return 0.25 * torch.log(3 * x**2 + 6 * x + 1) \
                - np.sqrt(6) / 24 \
                * torch.log((x + 1 - np.sqrt(2./3.)) / (x + 1 + np.sqrt(2./3.)))

    def get_peak(self, y):
        """
        Use Quinn 2nd estimator to get peak ybin
        """
        argmax = torch.argmax(torch.abs(y))
        argmax_pos = argmax + 1 if argmax != len(y) - 1 else 0
        argmax_neg = argmax - 1 if argmax != 0 else -1
        cast = torch.real if torch.is_complex(y) else torch.as_tensor
        rpos = cast(y[argmax_pos] / y[argmax])
        rneg = cast(y[argmax_neg] / y[argmax])
        dpos = -rpos / (1 - rpos)
        dneg = rneg / (1 - rneg)
        max_bin = argmax + ((dneg + dpos) / 2 + self.k(dneg**2) - self.k(dpos**2))

        return self.start + max_bin * self.dx

    def _iter_peak(self, inp, dim, out):
        if inp.ndim == 1:
            # estimate peak
            out[:] = self.get_peak(inp)
        else:
            # iterate
            for i in range(len(inp)):
                self._iter_peak(inp[i], dim+1, out[i])

    def forward(self, inp):

        if isinstance(inp, (dataset.VisData, dataset.MapData)):
            out = inp.copy()
            out.data = self.forward(inp.data)
            return out

        # take fft
        inp = super().forward(inp)

        # iterate over all dims
        shape = list(inp.shape)
        shape[self.dim] = 1
        out = torch.zeros(shape, dtype=utils._float())
        out = out.moveaxis(self.dim, -1)
        self._iter_peak(inp, 0, out)
        out = out.moveaxis(-1, self.dim)

        return out

