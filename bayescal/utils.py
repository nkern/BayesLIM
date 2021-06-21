"""
Utility module
"""
import numpy as np
import torch
from scipy import special
from scipy.special import spherical_jn as jn
from scipy.special import spherical_yn as yn
import copy
import warnings

from .data import DATA_PATH

# try to import healpy
try:
    import healpy
    import_healpy = True
except ImportError:
    import_healpy = False
if not import_healpy:
    try:
        # note this will have more limited capability
        # than healpy, but can do what we need
        from astropy_healpix import healpy
        import_healpy = True
    except ImportError:
        warnings.warn("could not import healpy")


def set_dtype(real_dtype):
    """
    Set the global torch data type.
    The complex dtype is 2 x real_dtype

    Parameters
    ----------
    real_dtype : torch.dtype
        The default dtype for real tensors
    """
    pass


########################################
######### Linear Algebra Tools #########
########################################

viewreal = torch.view_as_real
viewcomp = torch.view_as_complex
D2R = np.pi / 180


def cmult(a, b):
    """
    Complex multiplication of two real-valued torch
    tensors in "2-real" form, or of shape (..., 2)
    where the last axis indexes the real and imag
    component of the tensor, respectively.

    Parameters
    ----------
    a : tensor
        In 2-real form
    b : tensor
        In 2-real form

    Returns
    -------
    tensor
        Complex product of a and b in 2-real form
    """
    return viewreal(viewcomp(a) * viewcomp(b))


def cdiv(a, b):
    """
    Complex division (a / b) of two real-valued torch
    tensors in "2-real" form, or of shape (..., 2)
    where the last axis indexes the real and imag
    component of the tensor, respectively.

    Parameters
    ----------
    a : tensor
        In 2-real form
    b : tensor
        In 2-real form

    Returns
    -------
    tensor
        Complex division of a / b in 2-real form
    """
    return viewreal(viewcomp(a) / viewcomp(b))


def cconj(z):
    """
    Complex conjugate of a real-valued torch
    tensor in "2-real" form, or of shape (..., 2)
    where the last axis indexes the real and imag
    component of the tensor, respectively.

    Parameters
    ----------
    z : tensor
        In 2-real form

    Returns
    -------
    tensor
        Complex conjugate of z in 2-real form
    """
    return viewreal(viewcomp(z).conj())


def ceinsum(equation, *operands):
    """
    A shallow wrapper around torch.einsum,
    taking 2-real operands and returning
    2-real output.

    Parameters
    ----------
    equation : str
        A torch.einsum equation
    operands : tensor
        torch tensors to operate on in 2-real form

    Returns
    -------
    tensor
        Output of einsum in 2-real form
    """
    raise NotImplementedError("pytorch doesn't yet support complex autodiff for this")
    operands = (viewcomp(op) for op in operands)
    return viewreal(torch.einsum(equation, *operands))


def cinv(z):
    """
    Take the inverse of z
    across the first two axes

    Parameters
    ----------
    z : tensor
        torch tensor in 2-real form

    Returns
    -------
    tensor
        inverse of z in 2-real form
    """
    return viewreal(torch.inverse(viewcomp(z).T).T)


def diag_matmul(a, b):
    """
    Multiply two diagonal 1x1 or 2x2 matrices manually.
    This is generally faster than matmul or einsum
    for large, high dimensional stacks of 2x2 matrices.

    !! Note: this ignores the off-diagonal for 2x2 matrices !!
    If you need off-diagonal components, you are
    better off using torch.matmul or torch.einsum directly.

    Parameters
    ----------
    a, b : tensor
        of shape (Nax, Nax, ...), where Nax = 1 or 2

    Returns
    -------
    c : tensor
        of shape (Nax, Nax, ...)
    """
    if a.shape[0] == 1:
        # 1x1: trivial
        return a * b
    elif a.shape[0] == 2:
        # 2x2
        c = torch.zeros_like(a)
        c[0, 0] = a[0, 0] * b[0, 0]
        c[1, 1] - a[1, 1] * b[1, 1]
        return c
    else:
        raise ValueError("only 1x1 or 2x2 tensors")


def angle(z):
    """
    Compute phase of the 2-real tensor z

    Parameters
    ----------
    z : tensor
        In 2-real form

    Returns
    -------
    float or ndarray
        Phase of z in radians
    """
    return torch.angle(viewcomp(z))


def abs(z):
    """
    Take the abs of a 2-real tensor z.

    Parameters
    ----------
    z : tensor
        In 2-real form

    Returns
    -------
    tensor
        The amplitude of the input 2-real tensor
        projected into the complex plane with
        zero phase

    """
    zabs = torch.clone(z)
    zabs[..., 0] = torch.linalg.norm(z, axis=-1)
    zabs[..., 1] = 0
    return zabs


def apply_phasor(z, phi):
    """
    Apply a complex phasor to z

    Parameters
    ----------
    z : tensor
        In 2-real form
    phi : float
        Phase of phasor in radians

    Returns
    -------
    tensor
        z in 2-real form with phi applied
    """
    return viewreal(viewcomp(z) * np.exp(1j * phi))


def project_out_phase(z, avg_axis=None, select=None):
    """
    Compute and project out the phase of z

    Parameters
    ----------
    z : tensor
        In 2-real form
    avg_axis : int, optional
        Average z along avg_axis before computing
        its phase. Default is None.
    select : list, optional
        Use this to index z after any averaging
        before computing the phase.
        E.g.: select = [slice(None), slice(0, 1)].
        Note that this indexing must keep z's dimensionality.
        Default is None.

    Returns
    -------
    tensor
        z in 2-real form with phase projected out
    """
    if avg_axis is not None:
        za = torch.mean(z, axis=avg_axis, keepdim=True)
    else:
        za = z
    if select is not None:
        za = z[select]
    z_phs = angle(za)

    return apply_phasor(z, -z_phs)


def ones(*args, **kwargs):
    """
    Construct a 2-real tensor of ones

    Parameters
    ----------
    shape : tuple
        Shape of tensor

    Returns
    -------
    tensor
        A 2-real tensor full of ones

    Notes
    -----
    keyword arguments passed to torch.ones
    """
    ones = torch.ones(*args, **kwargs)
    ones[..., 1] = 0
    return ones


def cmatmul(a, b):
    """
    Perform 1x1 or 2x2 matrix multiplication
    along the first two axes of a and b
    in 2-real form. Note: this is slow
    compared to torch.einsum, but doesn't need
    to cast to complex

    Parameters
    -----------
    a : tensor
        In 2-real form with shape of b
    b : tensor
        In 2-real form with shape of a

    Returns
    -------
    tensor
        Matrix multiplication of a and b along
        their 0th and 1st axes
    """
    # determine if 1x1 or 2x2 matmul
    assert b.shape[0] == b.shape[1] == a.shape[0] == a.shape[1]
    assert a.shape[0] in [1, 2]
    twodim = True if a.shape[0] == 2 else False

    if not twodim:
        # 1x1 matmul is trivial
        return cmult(a, b)
    else:
        # 2x2 matmul
        c = torch.zeros_like(a)

        # upper left real
        c[0, 0, ..., 0] = a[0, 0, ..., 0] * b[0, 0, ..., 0] - a[0, 0, ..., 1] * b[0, 0, ..., 1] \
                          + a[0, 1, ..., 0] * b[1, 0, ..., 0] - a[0, 1, ..., 1] * b[1, 0, ..., 1]

        # upper left imag
        c[0, 0, ..., 1] = a[0, 0, ..., 0] * b[0, 0, ..., 1] + a[0, 0, ..., 1] * b[0, 0, ..., 0] \
                          + a[0, 1, ..., 0] * b[1, 0, ..., 1] + a[0, 1, ..., 1] * b[1, 0, ..., 0]

        # upper right real
        c[0, 1, ..., 0] = a[0, 0, ..., 0] * b[0, 1, ..., 0] - a[0, 0, ..., 1] * b[0, 1, ..., 1] \
                          + a[0, 1, ..., 0] * b[1, 1, ..., 0] - a[0, 1, ..., 1] * b[1, 1, ..., 1]

        # upper right imag
        c[0, 1, ..., 1] = a[0, 0, ..., 0] * b[0, 1, ..., 1] + a[0, 0, ..., 1] * b[0, 1, ..., 0] \
                          + a[0, 1, ..., 0] * b[1, 1, ..., 1] + a[0, 1, ..., 1] * b[1, 1, ..., 0]

        # lower left real
        c[1, 0, ..., 0] = a[1, 0, ..., 0] * b[0, 0, ..., 0] - a[1, 0, ..., 1] * b[0, 0, ..., 1] \
                          + a[1, 1, ..., 0] * b[1, 0, ..., 0] - a[1, 1, ..., 1] * b[1, 0, ..., 1]

        # lower left imag
        c[1, 0, ..., 1] = a[1, 0, ..., 0] * b[0, 0, ..., 1] + a[1, 0, ..., 1] * b[0, 0, ..., 0] \
                          + a[1, 1, ..., 0] * b[1, 0, ..., 1] + a[1, 1, ..., 1] * b[1, 0, ..., 0]

        # lower right real
        c[1, 1, ..., 0] = a[1, 0, ..., 0] * b[0, 1, ..., 0] - a[1, 0, ..., 1] * b[0, 1, ..., 1] \
                          + a[1, 1, ..., 0] * b[1, 1, ..., 0] - a[1, 1, ..., 1] * b[1, 1, ..., 1]

        # lower right imag
        c[1, 1, ..., 1] = a[1, 0, ..., 0] * b[0, 1, ..., 1] + a[1, 0, ..., 1] * b[0, 1, ..., 0] \
                          + a[1, 1, ..., 0] * b[1, 1, ..., 1] + a[1, 1, ..., 1] * b[1, 1, ..., 0]


    return c


######################################
######### Sky Modeling Tools #########
######################################

def colat2lat(theta, deg=True):
    """
    Convert colatitude to latitude and vice versa

    Parameters
    ----------
    theta : ndarray
        Colatitude
    deg : bool, optional
        If True, theta is in deg, otherwise in rad

    Returns
    -------
    ndarray
        Converted angles
    """
    if deg:
        return 90 - theta
    else:
        return np.pi / 2 - theta


def gen_lm(lmax, real_field=True):
    """
    Generate array of l and m parameters.
    Matches healpy.sphtfunc.Alm.getlm order.

    Parameters
    ----------
    lmax : int
        Maximum l parameter
    real_field : bool, optional
        If True, treat sky as real-valued (default)
        so truncate negative m values.

    Returns
    -------
    l, m : array_like
        array of shape (2, Ncoeff) holding
        the (l, m) parameters.
    """
    lms = []
    lowm = 0 if real_field else -lmax
    for m in range(lowm, lmax + 1):
        for l in range(0, lmax + 1):
            if np.abs(m) > l: continue
            lms.append([l, m]) 
    return np.array(lms).T


def gen_sph2pix(theta, phi, l=None, m=None, lmax=None, real_field=True,
                dtype=torch.complex64, device=None):
    """
    Generate spherical harmonic forward model matrix.
    Note for lmax > 50, this can begin to take >= minutes to run.

    Parameters
    ----------
    theta : array_like
        Co-latitude (i.e. zenith angle) [rad]
    phi : array_like
        Longitude (i.e. azimuth) [rad]
    l : array_like, optional
        Integer array of spherical harmonic l modes
    m : array_like, optional
        Integer array of spherical harmonic m modes
    lmax : int, optional
        If l, m are None, this generates l and m
        arrays
    real_field : bool, optional
        If True, treat sky as real-valued
        so truncate negative m values (used for lmax).
    device : str, optional
        Device to push Ylm to.
    dtype : dtype, optional
        Data type of output matrix.

    Returns
    -------
    Ylm : array_like
        An Npix x Ncoeff matrix encoding a spherical
        harmonic transform from a_lm -> map
    """
    if lmax is not None:
        l, m = gen_lm(lmax, real_field=real_field)
    torch_type = type(dtype) == torch.dtype

    if torch_type:
        Y = torch.zeros(len(theta), len(l), dtype=dtype)
    else:
        Y = np.zeros((len(theta), len(l)), dtype=dtype)

    # iterate over coefficients
    for i, (_l, _m) in enumerate(zip(l, m)):
        y = special.sph_harm(_m, _l, phi, theta)
        if torch_type:
            y = torch.as_tensor(y, dtype=dtype, device=device)
        Y[:, i] = y

    return Y


def gen_bessel2freq(l, freqs, cosmo, Nk=None, method='default', kbin_file=None,
                    decimate=True, dtype=torch.float32, device=None):
    """
    Generate spherical Bessel forward model matrices k^2 j_l(kr)
    from Fourier domain (k) to LOS distance or frequency domain (r_nu)

    The inverse transformation from Fourier space (k)
    to configuration space (r) is

    .. math::

        T_{lm}(r) &= \frac{2}{\pi} \int dk k^2 j_l(k r) T_{lm}(k) \\
        T(r,\theta,\phi) &= \frac{2}{\pi} \int dk k^2 j_l(k r)
                            T_l(k,\theta,\phi)

    following convention of Liu, Zhang, & Parsons 2016

    Parameters
    ----------
    l : array_like
        Spherical harmonic l modes for j_l(kr) terms
    freqs : array_like
        Frequency array [Hz]
    cosmo : Cosmology object
        For freq -> r [comoving Mpc] conversion
    Nk : int, optional
        Number of modes to compute, starting at smallest
    method : str, optional
        Method for constraining radial basis functions.
        options=['default', 'samushia', 'gebhardt']
        See sph_bessel_kn for details.
    decimate : bool, optional
        Use every other j_l(z) zero as k bins (i.e. DFT convention)
    device : str, optional
        Device to push j_l(kr) to.

    Returns
    -------
    jl : dict
        A dictionary holding a series of Nk x Nfreqs
        spherical Fourier Bessel transform matrices,
        one for each unique l mode.
        Keys are l mode integers, values are matrices.
    k : ndarray
        k modes [Mpc^-1]
    """
    # convert frequency to LOS distance
    r = cosmo.f2r(freqs)
    r_min, r_max = r.min(), r.max()
    # setup dicts
    jl = {}
    kbins = {}
    # configure 
    torch_type = type(dtype) == torch.dtype
    dr = r - r.min()
    if Nk is None:
        Nk = len(r) // 2 
    for _l in np.unique(l):
        k = sph_bessel_kn(_l, r_max, Nk, r_min=r_min, decimate=decimate,
                          method=method, filepath=kbin_file)
        if torch_type:
            j = torch.zeros(Nk, len(r), dtype=dtype, device=device)
        else:
            j = np.zeros((Nk, len(r)), dtype=dtype)
        for i, _k in enumerate(k):
            if method == 'default':
                # just j_l(kr)
                j_i = np.sqrt(2 / np.pi) * _k**2 * jn(_l, _k * r)
            elif method == 'samushia':
                # j_l(kr) + A y_l(kr)
                A = -jn(_l, _k * r_min) / yn(_l, _k * r_min)
                j_i = np.sqrt(2 / np.pi) * _k**2 \
                     * (jn(_l, _k * r) + A * yn(_l, _k * r))

            elif method == 'gebhardt':
                raise NotImplementedError

            if torch_type:
                j[i] = torch.as_tensor(j_i, dtype=dtype, device=device)
            else:
                j[i] = j_i
        jl[_l] = j
        kbins[_l] = k

    return jl, kbins


def sph_bessel_kln(l, r_max, Nk, r_min=None, decimate=True,
                   method='default', filepath=None):
    """
    Get spherical bessel Fourier bins given method.

    Parameters
    ----------
    l : int
        Angular l mode
    r_max : float
        Maximum survey radial extent [cMpc]
    Nk : int
        Number of k bins, starts with
        smallest modes and works up
    r_min : float, optional
        Survey starting boundary [cMpc]
        only used for special method
    decimate : bool, optional
        If True, use every other zero
        starting at the second zero. This
        is consistent with Fourier k convention.
    method : str, optional
        Method by which to generate k_ln spectrum.
        default : interval is 0 -> r_max, basis is
            j_l(kr), BC is j_l(k_ln r_max) = 0
        samushia : interval is r_min -> r_max, basis is
            g_nl = j_l(k_ln r) + A_ln y_l(k_ln r)
            and BC is g_nl(k r) = 0 for r_min and r_max
        gebhardt : interval is r_min -> r_max, basis is
            g_nl = j_l(k_ln r) + A_ln y_l(k_ln r)
            BC is potential field continuity (Gebhardt+2021)
    filepath : str, optional
        filepath to csv of kbins [cMpc^-1] in form of
        l, 1st zero, 2nd zero, 3rd zero, ...
        This supercedes method if passed.

    Returns
    -------
    array
        Fourier modes k_n = [2pi / r_n]
    """
    # get pre-computed k bins
    if filepath is not None:
        k = np.loadtxt(filepath, delimiter=',')[l, 1:]
    else:
        if method == 'default':
            import mpmath
            zeros = [float(mpmath.besseljzero(l+.5, k) for k in range(1, Nk+1))]
            k = np.array(zeros) / r_max

        elif method == 'samushia':
            kmin = 2 * np.pi / (r_max - r_min)
            dk = kmin / 500
            k_arr = dk + np.arange(0, 30000) * dk
            y = (jn(l, k_arr * r_min) * yn(l, k_arr * r_max) \
                 - jn(l, k_arr * r_max) * yn(l, k_arr * r_min)) * k_arr**2
            k = get_zeros(k_arr, y)

        elif method == 'gebhardt':
            raise NotImplementedError

    # compute k and decimate if desired
    if decimate:
        k = k[1::2]

    return k


def gen_poly_A(freqs, Ndeg, dtype=torch.float32, device=None):
    """
    Generate design matrix (A) for polynomial of Ndeg across freqs,
    with coefficient ordering

    .. math::

        a0 * x^0 + a1 * x^1 + a2 * x^2 + ...

    Parameters
    ----------
    freqs : ndarray
        Frequency bins [Hz]
    Ndeg : int
        Polynomial degree
    device : str
        device to send A matrix to

    Returns
    -------
    torch tensor
        Polynomial design matrix
    """
    dfreqs = (freqs - freqs[0]) / 1e6  # In MHz
    A = torch.tensor([dfreqs**i for i in range(Ndeg)], dtype=dtype, device=device).T
    return A



def voigt_beam(nside, sigma, gamma):
    """
    A power beam with a Voigt profile

    Parameters
    ----------
    nside : int
        HEALpix nside parameter
    sigma ; float
        Standard deviation of Gaussian component [rad]
    gamma : float
        Half-width at half-max of Cauchy component [rad]

    Returns
    -------
    beam
        HEALpix map (ring ordered) of Voigt beam
    theta, phi
        co-latitude and longitude of HEALpix map [rad]
    """
    theta, phi = healpy.pix2ang(nside, np.arange(healpy.nside2npix(nside)))
    beam = special.voigt_profile(theta, sigma, gamma)
    beam /= beam.max()

    return beam, theta, phi


def _value_fun(start, stop, hp_map):
    value = sum(hp_map._data[start:stop])
    if hp_map._density:
        value /= stop - start
    return value


def adaptive_healpix_mesh(hp_map, split_fun=None):
    """
    Convert a single resolution healpix map to a
    multi-order coverage (MOC) map based on
    mhealpy's pixel value algorithm (density = False)

    Parameters
    ----------
    hp_map : mhealpy.HealpixBase subclass
        single resolution map to convert to multi-resolution
        based on relative pixel values and split_fun.
        Note that this should have density = False.
    split_fun : callable
        Function that determines if a healpix pixel is split into
        multiple pixels. See mhealpy.adaptive_moc_mesh().
        Default is mhealpy default function.

    Returns
    -------
    grid : HealpixMap object
        Downsampled healpix grid. Note that, due to how
        mhealpy.get_interp_val works, this will have density = True.
    theta, phi : array_like
        Co-latitude and longitude of downsampled map [rad]

    Notes
    -----
    See multires_map for downsampling a sky map onto
    output grid.
    """
    # set split_fun
    if split_fun is None:
        def split_fun(start, stop):
            max_value = max(hp_map)
            return _value_fun(start, stop, hp_map) > max_value

    # convert to nested if ring
    if hp_map.is_ring:
        ring2nest = healpy.ring2nest(hp_map.nside,
                                     np.arange(healpy.nside2npix(hp_map.nside)))
        hp_map._data = hp_map._data[np.argsort(ring2nest)]
        hp_map._scheme = 'NESTED'

    # downsample healpix map grid
    grid = hp_map.adaptive_moc_mesh(hp_map.nside, split_fun,
                                    dtype=hp_map.dtype)
    grid._density = True

    # fill data array
    rangesets = grid.pix_rangesets(grid.nside)
    for pix,(start, stop) in enumerate(rangesets):
        grid[pix] = _value_fun(start, stop, hp_map)

    # get theta, phi arrays
    theta, phi = grid.pix2ang(np.arange(grid.npix))

    return grid, theta, phi 


def multires_map(hp_map, grid, weights=None, dtype=None):
    """
    Given a multi-resolution grid, downsample
    a singe-res healpix map to multi-res grid.

    Parameters
    ----------
    hp_map : array_like or mhealpy.HealpixMap object
        A single-res healpix map to downsample (NESTED)
        If array_like, the last axis must be sky pixel axis
    grid : mhealpy.HealpixMap object
        Multi-resolution HealpixMap object containing
        grid to downsample to.
    weights : array_like or mhealpy.HealpixMap object, optional
        Optional weights to use when averaging
        child pixels of hp_map within a parent
        pixel in grid. Must be same nside as hp_map.
    dtype : object
        Data type of output map. Default is grid.dtype.

    Returns
    -------
    hp_map_mr
        Multiresolution healpix object of hp_map.
    """
    if isinstance(grid, mhealpy.HealpixBase):
        hp_map_mr = copy.deepcopy(grid)
        hp_map_mr._data = hp_map_mr._data.astype(dtype)
        nside = hp_map.nside
    else:
        hp_map_mr = np.zeros(hp_map.shape[:-1] + grid.data.shape,
                             dtype=dtype)
        nside = healpy.npix2nside(hp_map.shape[-1])

    # average hp_map
    for i, rs in enumerate(grid.pix_rangesets(nside)):
        # get weights
        w = np.ones(rs[1] - rs[0])
        if weights is not None:
            w = weights[..., rs[0]:rs[1]]
        # take average of child pixels
        hp_map_mr[..., i] = np.sum(hp_map[..., rs[0]:rs[1]] * w, axis=-1) / np.sum(w, axis=-1).clip(1e-40, np.inf)

    return hp_map_mr


def _recursive_pixelization(bsky, prev_ind, prev_nside, max_nside, theta, phi, nsides, total_nsides,
                           sigma=None, target_nside=None):
    """
    A dynamic pixelization scheme. See dynamic_pixelization() for operation.

    Parameters
    ----------
    bsky : array_like
        beam weighted healpix sky in NEST order (at high nside resolution)
    prev_ind : int
        HEALpix index of the leaf we are currently subdividing
    prev_nside : int
        HEALpix nside of the leaf we are currently subdividing
    max_nside : int
        Maximum nside of dynamic pixelization.
    theta, phi, nsides, total_nsides: list
        Empty lists to append to
    sigma : float, optional
        Sigma threshold for beam weighted pixelization
    target_nside : int
        The nside assigned to the leaf we are currently subdividign.
    """
    # get new nside
    this_nside = prev_nside * 2
    # determine if prev_nside is enough
    if (prev_nside >= max_nside) or (target_nside is not None and prev_nside >= target_nside):
        angs = healpy.pix2ang(prev_nside, prev_ind, nest=True)
        theta.append(angs[0])
        phi.append(angs[1])
        nsides.append(prev_nside)
        total_nsides.extend([prev_nside] * int(4**(np.log(max_nside / this_nside) / np.log(2) + 1)))
        return
    # get the four indices of this leaf in this_nside nest ordering
    start_ind = 4 * prev_ind
    inds = range(start_ind, start_ind + 4)
    # get the bsky interpolated values
    angs = healpy.pix2ang(this_nside, inds, nest=True)
    # figure out if we need to subdivide or not
    if sigma is not None:
        vals = healpy.get_interp_val(bsky, *angs, nest=True)
        stop_divide = np.std(vals) < sigma
    if this_nside >= max_nside:
        stop_divide = True
    if target_nside is not None:
        stop_divide = this_nside >= target_nside
    if stop_divide:
        theta.extend(angs[0])
        phi.extend(angs[1])
        nsides.extend([this_nside] * 4)
        total_nsides.extend([this_nside] * int(4**(np.log(max_nside / this_nside) / np.log(2) + 1)))
    # otherwise, iterate over each leaf and subdivide again
    else:
        for ind in inds:
            _recursive_pixelization(bsky, ind, this_nside, max_nside, theta, phi, nsides, total_nsides,
                                    sigma=sigma, target_nside=target_nside)


def nside_binning(zen, ra, zen_sigma=5, zen_gamma=15, ra_sigma=5, ra_gamma=15,
                  ra_min_max=None, min_nside=32, max_nside=256):
    """
    Compute nside binning using a voigt profile given
    a map of sky angles. Note for the ra axis: be mindful
    of how the ra_min_max cuts depend on the wrapping of
    the input ra array.

    Parameters
    ----------
    zen : array_like
        Zenith sky coordinate along the declination axis [deg].
    ra : array_like
        Right ascension coordinate [deg]. 
    zen_sigma, zen_gamma : float
        Sigma and gamma parameters of voigt profile of the
        zenith angle along the declination axis [deg]
    ra_sigma, ra_gamma : float
        Sigma and gamma parameters of voigt profile
        along right ascension [deg]
    ra_min_max : 2-tuple
        Minimum and maximum ra [deg] cut to keep a flat, max
        nside response of the binning. Outside of these cuts
        the voigt profile parameters lower the nside resolution.
    min_nside : int
        Minimum nside resolution. Must be a power of 2.
    max_nside : int
        Maximum nside resolution. Must be a power of 2.

    Returns
    -------
    curve : array_like
        Voigt profile curve used to set the nside binning
    nside_bins : array_like
        The nside of each pixel on the sky
    """
    # get zen component of voigt profile
    curve = special.voigt_profile(zen, zen_sigma, zen_gamma)
    curve -= curve.min()
    curve /= curve.max()

    # get ra component of voigt profile
    if ra_min_max is not None:
        # enact a nside res decay for ra less than min ra
        assert ra_min_max[0] > ra.min()
        ra_low = ra < ra_min_max[0]
        ra_low_curve = special.voigt_profile(ra[ra_low] - ra_min_max[0], ra_sigma, ra_gamma)
        ra_low_curve -= ra_low_curve.min()
        ra_low_curve /= ra_low_curve.max()
        curve[ra_low] *= ra_low_curve
        # enact a nside res decay for ra greater than max ra
        ra_hi = ra > ra_min_max[1]
        assert ra_min_max[1] < ra.max()
        ra_hi_curve = special.voigt_profile(ra[ra_hi] - ra_min_max[1], ra_sigma, ra_gamma)
        ra_hi_curve -= ra_hi_curve.min()
        ra_hi_curve /= ra_hi_curve.max()
        curve[ra_hi] *= ra_hi_curve

    # normalize curve to max and min_nside
    curve *= (max_nside - min_nside)
    curve += min_nside

    # bin the inputs
    bins = np.array([2 ** i for i in range(int(np.log(min_nside)/np.log(2)), int(np.log(max_nside)/np.log(2)) + 1)])
    inds = np.array([np.argmin(np.abs(bins - c)) for c in curve])
    nside_bins = np.array([bins[i] for i in inds])

    return curve, nside_bins


def dynamic_pixelization(base_nside, max_nside, sigma=None, bsky=None, target_nsides=None):
    """
    Two dynamic HEALpix pixelization schemes.
    1. Based on Zheng+2016 MITEOR Map Making (sigma)
    2. Manual pixelization (set by target_nsides)

    Parameters
    ----------
    base_nside : int
        The starting, minimum nside of the map. Must be power of 2.
    max_nside : int
        The upper limit on nside resolution. Must be a power of 2.
    sigma : float, optional
        If using algorithm (1), this is the standard deviation
        threshold of the bsky map, above which the healpix pixel
        is subdivded, below which the pixelization stops.
    bsky : array_like, optional
        If using algorithm (1), this is the beam weighted sky (NEST)
        to compute the standard deviations. This should be fed
        at an nside resolution higher than max_nside.
    target_nsides : array_like, optional
        If using algorithm (2), this should be an array of integers
        that has a length nside2npix(base_nside). Each element
        sets the nside resolution of that healpix pixel.
        See nside_binning() for examples.

    Returns
    -------
    theta, phi : array_like
        Co-latitude and longitude [radians] of dynamic pixels
    nsides : mhealpy HealpixBase object
        nside resolution of each pixel in theta, phi. This also
        holds the pixrangesets used in multires_map for downsampling
        a single-resolution healpix map to the dynamic res map.
    total_nsides : array_like
        An array that has the full shape of nside2npix(max_nside),
        with each element containing the nside resolution of the
        dynamic pixelization map at that location. This is used
        to plot the nside resolution of the map in healpix format.
    """
    import mhealpy
    theta, phi, nsides, total_nsides = [], [], [], []
    for i in range(healpy.nside2npix(base_nside)):
        target = target_nsides[i] if target_nsides is not None else None
        _recursive_pixelization(bsky, i, base_nside, max_nside, theta, phi, nsides, total_nsides,
                                sigma=sigma, target_nside=target)
    theta, phi, total_nsides = np.array(theta), np.array(phi), np.array(total_nsides)
    # turn nsides into mhealpy HealpixMap object
    ipix = [healpy.ang2pix(ns, th, ph, nest=True) for ns, th, ph in zip(nsides, theta, phi)]
    uniq = [4 * ns**2 + ip for ns, ip in zip(nsides, ipix)]
    nsides = mhealpy.HealpixMap(nsides, uniq=uniq, scheme='nested', dtype=np.int16)

    return theta, phi, nsides, total_nsides


#################################
######### Miscellaneous #########
#################################

def push(tensor, device):
    """
    Push a tensor to a new device. If the tensor
    is a parameter, it instantiates the parameter
    class on device.

    Parameters
    ----------
    tensor : tensor
        A pytorch tensor, optionally a pytorch Parameter
    device : str
        The device to push it to

    Returns
    -------
    tensor
        The tensor on device
    """
    if isinstance(tensor, torch.nn.Parameter):
        return torch.nn.Parameter(tensor.to(device))
    else:
        return tensor.to(device)

def tensor2numpy(tensor):
    """
    Convert a tensor (on any device)
    to a numpy ndarray on the cpu

    Parameters
    ----------
    tensor : tensor
        A pytorch tensor on any device

    Returns
    -------
    ndarray
        The tensor as an ndarray on cpu
    """
    if isinstance(tensor, np.ndarray):
        return tensor
    tensor = tensor.detach()
    if tensor.device != 'cpu':
        tensor = tensor.cpu()
    return tensor.numpy()


def fit_zero(x, y):
    """fit a quadratic and solve for roots"""
    a, b, c = np.polyfit(x, y, 2)
    d = b**2 - 4*a*c
    x1 = (-b + np.sqrt(d)) / (2 * a)
    x2 = (-b - np.sqrt(d)) / (2 * a)
    sol = x1 if np.abs(x1 - x[0]) < np.abs(x2 - x[0]) else x2
    return sol


def get_zeros(x, y):
    """iterate over y and get zeros"""
    # get roots
    roots = []
    for i in range(len(y)):
        if i == 0:
            continue
        if np.sign(y[i]) != np.sign(y[i-1]):
            # get 3 nn points and fit quadratic for root
            nn = np.argsort(np.abs(y)[i-3:i+3])[:3] + (i - 3)
            roots.append(fit_zero(x[nn], y[nn]))
            
    return roots

