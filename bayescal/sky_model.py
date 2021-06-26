"""
Module for torch sky models and relevant functions
"""
import torch
import numpy as np
from scipy import special

from . import utils


class SkyBase(torch.nn.Module):
    """
    Base class for various sky model representations
    """
    def __init__(self, params, kind, freqs, R=None, parameter=True):
        """
        Base class for a torch sky model representation.

        Parameters
        ----------
        params : tensor
            A sky model parameterization as a tensor to
            be pushed through the response function R().
        kind : str
            Kind of sky model. options = ['point', 'pixel', 'alm']
            for point source model, pixelized model, and spherical
            harmonic model.
        freqs : tensor
            Frequency array of sky model [Hz]
        R : callable, optional
            An arbitrary response function for the
            point source model, mapping self.params
            to a sky source tensor of shape
            (Npol, Npol, Nfreqs, Nsources)
        parameter : bool
            If True, treat params as variables to be fitted,
            otherwise hold them fixed as their input value
        """
        super().__init__()
        self.params = params
        if parameter:
            self.params = torch.nn.Parameter(self.params)
        self.kind = kind
        if R is None:
            R = DefaultResponse()
        self.R = R
        self.freqs = freqs
        self.Nfreqs = len(freqs)

    def push(self, device, attrs=[]):
        """
        Wrapper around nn.Module.to(device) method
        but always pushes self.params whether its a 
        parameter or not.

        Parameters
        ----------
        device : str
            Device to push to, e.g. 'cpu', 'cuda:0'
        attrs : list of str
            List of additional attributes to push
        """
        self.params = utils.push(self.params, device)
        if hasattr(self, 'angs'):
            self.angs = self.angs.to(device)
        for attr in attrs:
            setattr(self, attr, getattr(self, attr).to(device))


class DefaultResponse:
    """
    Default response function for SkyBase  
    """
    def __init__(self):
        pass

    def __call__(self, params):
        return params


class PointSourceModel(SkyBase):
    """
    Point source sky model with fixed
    source locations but variable flux density.
    Relates source flux parameterization
    to per-frequency, per-stokes, per-source
    flux density vector.

    Returns point source flux density and their sky
    locations in equatorial coordinates.
    """
    def __init__(self, params, angs, freqs, R=None, parameter=True):
        """
        Fixed-location point source model with
        parameterized flux density.

        Parameters
        ----------
        params : tensor
            Point source flux parameterization adopted by R().
            In general, this is of shape (Npol, Npol, Ncoeff, Nsources),
            where Ncoeff is the chosen parameterization across frequency.
            For no parameterization (default) this should be a tensor
            of shape (Npol, Npol, Nfreqs, Nsources).
            Npol is the number of feed polarizations, and
            the first two axes are the coherency matrix B:

            .. math::

                B = \left(
                    \begin{array}{cc}I + Q & U + iV \\
                    U - iV & I - Q \end{array}
                \right)

            See bayescal.sky.stokes2linear() for details.
        angs : tensor
            Point source unit vectors on the sky in equatorial
            coordinates of shape (2, Nsources), where the
            last two axes are RA and Dec [deg].
        freqs : tensor
            Frequency array of sky model [Hz].
        R : callable, optional
            An arbitrary response function for the
            point source model, mapping self.params
            to a sky source tensor of shape
            (Npol, Npol, Nfreqs, Nsources)
        parameter : bool, optional
            If True, treat params as parameters to be fitted,
            otherwise treat as fixed to its input value.

        Examples
        --------
        Here is an example for a simple point source model
        with a frequency power law parameterization.
        Note that the frequency array must be defined
        in the global namespace.

        .. code-block:: python

            Nfreqs = 16
            freqs = np.linspace(100e6, 120e6, Nfreqs)  # Hz
            phi = np.random.rand(100) * 180            # dec
            theta = np.random.rand(100) * 360          # ra
            angs = torch.tensor([theta, phi])
            amps = scipy.stats.norm.rvs(20, 1, 100)
            amps = torch.tensor(amps.reshape(1, 100, 1))
            alpha = torch.tensor([-2.2])
            def R(params, freqs=freqs):
                S = params[0][..., None]
                spix = params[1]
                return S * (freqs / freqs[0])**spix
            P = bayescal.sky.PointSourceModel([amps, alpha],
                                              angs, Nfreqs, R=R)

        """
        super().__init__(params, 'point', freqs, R=R, parameter=parameter)
        self.angs = angs

    def forward(self, params=None):
        """
        Forward pass the sky parameters

        Parameters
        ----------
        params : list of tensors, optional
            Set of parameters to use instead of self.params.

        Returns
        -------
        dictionary
            kind : str
                Kind of sky model ['point', 'pixel', 'alm']
            sky : tensor
                Source brightness at discrete locations
                (Npol, Npol, Nfreqs, Nsources)
            angs : tensor
                Sky source locations (RA, Dec) [deg]
                (2, Nsources)
        """
        # fed params or attr params
        if params is None:
            params = self.params

        # pass through response
        return dict(kind=self.kind, sky=self.R(params), angs=self.angs)


class PointSourceResponse:
    """
    Frequency parameterization of point sources at
    fixed locations but variable flux wrt frequency
    options include
        - channel : vary all frequency channels
        - poly : fit a low-order polynomial across freqs
        - powerlaw : fit an amplitude and exponent across freqs
    """
    def __init__(self, freqs, mode='poly', f0=None, dtype=torch.float32,
                 device=None, Ndeg=None):
        """
        Choose a frequency parameterization for PointSourceModel

        Parameters
        ----------
        freqs : tensor
            Frequency array [Hz]
        mode : str, optional
            options = ['channel', 'poly', 'powerlaw']
            Frequency parameterization mode. Choose between
            channel - each frequency is a parameter
            poly - polynomial basis of Ndeg
            powerlaw - amplitude and powerlaw basis anchored at f0
        f0 : float, optional
            Fiducial frequency [Hz]. Used for poly and powerlaw.
        dtype : torch dtype, optional
            Tensor data type of point source params
        device : str, optional
            Device of point source params
        Ndeg : int, optional
            Polynomial degrees if mode is 'poly'

        Notes
        -----
        The ordering of the coeff axis in params should be
            poly - follows that of utils.gen_poly_A()
            powerlaw - ordered as (amplitude, exponent)
        """
        self.freqs = freqs
        self.f0 = f0
        self.dfreqs = (freqs - freqs[0]) / 1e6  # MHz
        self.mode = mode
        self.Ndeg = Ndeg

        # setup
        if self.mode == 'poly':
            self.A = utils.gen_poly_A(self.dfreqs, Ndeg, device=device)

    def __call__(self, params):
        if self.mode == 'channel':
            return params
        elif self.mode == 'poly':
            return self.A @ params
        elif self.mode == 'powerlaw':
            return params[..., 0, :] * (self.freqs / self.f0)**params[..., 1, :]


class PixelModel(SkyBase):
    """
    Pixelized model (e.g. Healpix) of the sky
    specific intensity (aka brightness or temperature)
    at fixed locations in Equatorial coordinates
    but with variable amplitude.

    While the input sky model (params) should be in units of
    specific intensity (Kelvin or Jy / str), the output
    of the forward model is in flux density [Jy]
    (i.e. we multiply by each cell's solid angle).
    """
    def __init__(self, params, angs, freqs, px_area, R=None, parameter=True):
        """
        Pixelized model of the sky brightness distribution.
        This can be parameterized in any generic way via params,
        but the output of R(params) must be
        a representation of the sky brightness at fixed
        cells, which are converted to flux density
        by multiplying by each cell's solid angle.

        Parameters
        ----------
        params : tensor
            Sky model flux parameterization of shape
            (Npol, Npol, Nfreq_coeff, Nsky_coeff), where Nsky_coeff is
            the free parameters describing angular fluctations, and Nfreq_coeff
            is the number of free parameters describing frequency fluctuations,
            both of which should be expected by the response function R().
            By default, this is just Nfreqs and Npix, respectively.
            Npol is the number of feed polarizations.
            The first two axes are the coherency matrix B:

            .. math::

                B = \left(
                    \begin{array}{cc}I + Q & U + iV \\
                    U - iV & I - Q \end{array}
                \right)

            See bayescal.sky.stokes2linear() for details.
        angs : tensor
            Point source unit vectors on the sky in equatorial
            coordinates of shape (2, Nsources), where the
            last two axes are RA and Dec [deg].
        freqs : tensor
            Frequency array of sky model [Hz].
        px_area : float
            Contains the solid angle of each pixel [str]. This is multiplied
            into the final sky model, and thus needs to be a scalar or
            a tensor of shape (1, 1, 1, Npix) to allow for broadcasting
            rules to apply.
        R : callable, optional
            An arbitrary response function for the sky model, mapping
            self.params to a sky pixel tensor of shape
            (Npol, Npol, Nfreqs, Npix)
        parameter : bool, optional
            If True, treat params as parameters to be fitted,
            otherwise treat as fixed to its input value.
        """
        super().__init__(params, 'pixel', freqs, R=R, parameter=parameter)
        self.angs = angs
        self.px_area = px_area

    def forward(self, params=None):
        """
        Forward pass the sky parameters.

        Parameters
        ----------
        params : list of tensors, optional
            Set of parameters to use instead of self.params.

        Returns
        -------
        dictionary
            kind : str
                Kind of sky model ['point', 'pixel', 'alm']
            amps : tensor
                Pixel flux density at fixed locations on the sky
                (Npol, Npol, Nfreqs, Npix)
            angs : tensor
                Sky source locations (RA, Dec) [deg]
                (2, Npix)
        """
        # apply fed params or attr params
        if params is None:
            params = self.params

        # pass through response
        sky = self.R(params) * self.px_area
        return dict(kind=self.kind, sky=sky, angs=self.angs)


class PixelModelResponse:
    """
    Spatial and frequency parameterization for PixelModel

    options for spatial parameterization include
        - 'pixel' : sky pixel
        - 'alm' : spherical harmonic

    options for frequency parameterization include
        - 'channel' : frequency channels
        - 'poly' : low-order polynomials
        - 'powerlaw' : power law model
        - 'bessel' : spherical bessel j_l (for spatial mode 'alm')
            For this mode, the all elements in params must be
            from a single l mode
    """
    def __init__(self, theta, phi, freqs, spatial_mode='pixel', freq_mode='channel',
                 device=None, transform_order=0, dtype=torch.float32,
                 lms=None, f0=None, Ndeg=None, Nk=None, decimate=True, cosmo=None,
                 method='samushia', kbin_file=None):
        """
        Parameters
        ----------
        theta, phi : ndarrays
            colatitude and azimuth angles [radian] of the output sky map
            in arbitrary coordintes
        freqs : ndarray
            Frequency bins [Hz]
        spatial_mode : str, optional
            Choose the spatial parameterization (default is pixel)
        freq_mode : str, optional
            Choose the freq parameterization (default is channel)
        device : str, optional
            Device to put model on
        transform_order : int, optional
            0 - spatial then frequency transform (default)
            1 - frequency then spatial transform
        lms : ndarray, optional
            l and m modes for alm decomposition, shape (2, Ncoeff)
        f0 : float, optional
            Fiducial frequency [Hz], only used for polynomial basis
        kbins : ndarray, optional
            The wavevector bins used in the spherical bessel transform
        cosmo : Cosmology object
            Cosmology object for computing conversions
        method : str, optional
            If freq_mode is 'bessel', this is the radial basis method
        kbin_file : str, optional
            If freq_mode is 'bessel', this is a filepath to a csv of
            pre-computed k_ln bins.
        """
        self.theta, self.phi = theta, phi
        self.freqs = freqs
        self.spatial_mode = spatial_model
        self.freq_mode = freq_mode
        self.device = device
        self.transform_order = transform_order
        self.l, self.m = lms
        self.f0 = f0
        self.dfreqs = (freqs - freqs[0]) / 1e6
        self.Ndeg = Ndeg
        self.Nk = Nk
        self.decimate = decimate
        self.cosmo = cosmo
        self.dtype = dtype
        self.method = method
        self.kbin_file = kbin_file

        # freq setup
        self.A, self.j = None, None
        if self.freq_mode == 'poly':
            self.A = utils.gen_poly_A(self.dfreqs, Ndeg, device=self.device)
        elif self.freq_mode == 'bessel':
            # compute comoving line of sight distances
            self.z = cosmo.f2z(freqs)
            self.r = cosmo.comoving_distance(self.z).value
            self.dr = self.r = self.r.min()
            jl, kbins = utils.gen_bessel2freq(self.l, freqs, cosmo,
                                              Nk=Nk, decimate=decimate,
                                              device=device, dtype=dtype,
                                              method=method, kbin_file=kbin_file)
            self.jl = jl[list(jl.keys())[0]]
            self.kbins = kbins[list(kbins.keys())[0]]

        # spatial setup
        self.Ylm = None
        if self.spatial_mode == 'alm':
            self.Ylm = utils.gen_sph2pix(theta, phi, self.l, self.m, device=self.device,
                                         real_field=True, dtype=self.dtype)

        # assertions
        if self.freq_mode == 'bessel':
            assert self.spatial_mode == 'alm'
            assert len(np.unique(self.l)) == 1
            assert self.transform_order == 1

    def spatial_transform(self, params):
        """
        Forward model the sky params tensor
        through a spatial transform.

        Parameters
        ----------
        params : tensor
            Sky model parameters (Npol, Npol, Ndeg, Ncoeff)
            where Ndeg may equal Nfreqs, and Ncoeff
            are the coefficients for the sky representations.

        Returns
        -------
        tensor
            Sky model of shape (Npol, Npol, Ndeg, Npix)
        """
        if self.spatial_mode == 'pixel':
            return params
        elif self.spatial_mode == 'alm':
            return params @ self.Ylm.transpose(-1, -2)

    def freq_transform(self, params):
        """
        Forward model the sky params tensor
        through a frequency transform.

        Parameters
        ----------
        params : tensor
            Sky model parameters (Npol, Npol, Ndeg, Ncoeff)
            where Ncoeff may equal Npix, and Ndeg
            are the coefficients for the frequency representations.
    
        Returns
        -------
        tensor
            Sky model of shape (Npol, Npol, Nfreqs, Ncoeff)
        """
        if self.freq_mode == 'channel':
            return params
        elif self.freq_mode == 'poly':
            return self.A @ params
        elif self.freq_mode == 'powerlaw':
            return params[..., 0, :] * (self.freqs / self.f0)**params[..., 1, :]
        elif self.freq_mode == 'bessel':
            return (params.transpose(-1, -2) @ self.j).transpose(-1, -2)

    def __call__(self, params):
        if params.device != self.device:
            params = utils.push(params, self.device)
        if self.transform_order == 0:
            params = self.spatial_transform(params)
            params = self.freq_transform(params)
        else:
            params = self.freq_transform(params)
            params = self.spatial_transform(params)

        return params


class SphHarmModel(SkyBase):
    """
    Spherical harmonic expansion of a sky temperature field
    at pointing direction s and frequency f

    .. math::

        T(s, f) = \sum_{lm} = Y_{lm}(s) a_{lm}(f)

    where Y is a spherical harmonic of order l and m
    and t is its   coefficient.
    """
    def __init__(self, params, lms, freqs, R=None, parameter=True):
        """
        Spherical harmonic representation of the sky brightness.
        Can also accomodate a spherical Fourier Bessel model.

        Parameters
        ----------
        params : list of tensors
            Spherical harmonic parameterization of the sky.
            The first element of params must be a tensor holding
            the a_lm coefficients of shape
            (Npol, Npol, Nfreqs, Ncoeff). Nfreqs may also be
            replaced by Nk for a spherical Fourier Bessel model.
            Additional tensors can also parameterize frequency axis.
        lms : array
            Array holding spherical harmonic orders (l, m) of shape
            (2, Ncoeff).
        freqs : tensor
            Frequency array of sky model [Hz].
        R : callable, optional
            An arbitrary response function for the
            spherical harmonic model, mapping input self.params
            to an output a_lm basis of shape
            (Npol, Npol, Nfreqs, Ncoeff).
        parameter : bool, optional
            If True, treat params as parameters to be fitted,
            otherwise treat as fixed to its input value.
        """
        raise NotImplementedError


class CompositeModel(torch.nn.Module):
    """
    Multiple sky models, possibly on different devices
    """
    def __init__(self, models, sum_output=False, device=None):
        """
        Multiple sky models to be evaluated
        and returned in a list

        Parameters
        ----------
        models : list
            List of sky model objects
        sum_output : bool, optional
            If True, sum output sky model from
            each model before returning. This only
            works if each input model is of the
            same kind, and if they have the same
            shape.
        device : str, optional
            Device to move output to before summing
            if sum_output
        """
        self.models = models
        self.sum_output = sum_output
        self.device = device

    def forward(self, models=None):
        """
        Forward pass sky models and append in a list

        Parameters
        ----------
        models : list
            List of sky models to use instead of self.models

        Returns
        -------
        list
            List of each sky model output or their sum
        """
        if models is not None:
            models = self.models

        sky_models = [mod.forward() for mod in models]
        if self.sum_output:
            # assert only one kind of sky models
            assert len(set([mod['kind'] for mod in models])) == 1
            output = sky_models[0]
            output['sky'] = torch.sum([utils.push(mode['sky'], self.device) for mode in sky_models], axis=0)
            # make sure other keys are on the same device
            for k in output:
                if isinstance(output[k], torch.Tensor):
                    if output[k].device.type != self.device:
                        output[k] = output[k].to(self.device)

        return output


def stokes2linear(stokes):
    """
    Convert Stokes parameters to coherency matrix
    for xyz cartesian (aka linear) feed basis.
    This can be included at the beginning of
    the response matrix (R) of any of the sky model
    objects in order to properly account for Stokes
    Q, U, V parameters in your sky model.

    Parameters
    ----------
    stokes : tensor
        Holds the Stokes parameter of your generalized
        sky model parameterization, of shape (4, ...)
        with the zeroth axis holding the Stokes parameters
        in the order of [I, Q, U, V].

    Returns
    -------
    B : tensor
        Coherency matrix of electric field in xyz cartesian
        basis of shape (2, 2, ...) with the form

        .. math::

            B = \left(
                \begin{array}{cc}I + Q & U + iV \\
                U - iV & I - Q \end{array}
            \right)
    """
    B = torch.zeros(2, 2, stokes.shape[1:])
    B[0, 0] = stokes[0] + stokes[1]
    B[0, 1] = stokes[2] + 1j * stokes[3]
    B[1, 0] = stokes[2] - 1j * stokes[3]
    B[1, 1] = stokes[0] - stokes[1]

    return B


def parse_catalogue(catfile, parameter=False):
    """
    Read a point source catalogue YAML file.
    See bayescal.data.DATA_PATH for examples.

    Parameters
    ----------
    catfile : str
        Path to a YAML point source catalogue file

    Returns
    -------
    tensor
        PointSourceModel object
    """
    import yaml
    with open(catfile) as f:
        d = yaml.load(d, Loader=yaml.FullLoader)

    raise NotImplementedError
    """
    R = PointSourceResponse(d['freqs'], mode=f['mode'])
    S = PointSoureModel(params, angs, freqs, R=R, parameter=parameter)
    """
    return S

