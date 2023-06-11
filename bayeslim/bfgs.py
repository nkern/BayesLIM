"""
BFGS minimizer and its limited-memory counterpart (L-BFGS),
loosely modeled after PyTorch's LBFGS implementation [2].

[1] Nocedal & Wright, "Numerical Optimization", (2000) 2nd Ed.
[2] https://pytorch.org/docs/stable/_modules/torch/optim/lbfgs.html
[3] Jiang, Byrd, Eskow & Schnabel, "Preconditioned LBFGS", (2004)
"""
from abc import abstractmethod
from collections import deque
import numpy as np
import torch
from torch.optim.optimizer import Optimizer

from . import optim, hessian, paramdict


class BFGS:
    """
    The BFGS algorithm described in [1].
    This stores a dense approximation to the full NxN inverse
    Hessian matrix as self.H. Allows for a starting sparse or dense inverse Hessian.

    Notes:
        - no per-parameter options
        - all parameters must be on a single device

    At each optimization step, we update the approx inverse Hessian (H) as

        H_k+1 = V_k^T H_k V_k + rho_k s_k s_k^T

    where
        matrix      V_k = (I - rho_k y_k s_k^T)
        col-vector  s_k = x_k+1 - x_k
        col-vector  y_k = grad_k+1 - grad_k
        scalar     rho_k = 1 / (y_k^T s_k)

    and H_{k=0} = y_k @ y_k / y_k @ s_k if an initial H0 is not provided.

    Then the parameter position update is given as

        x_k+1 = x_k + alpha_k p_k

    where

        p_k = -H_k grad_k

    where again H_k is the inverse Hessian approximation N x N matrix at step k and grad_k
    are the loss function gradients column-vector N x 1, and alpha_k is a scalar coefficient
    determined by line search. H0 is specified by the user or is the identity matrix.

    [1] Nocedal & Wright, "Numerical Optimization", (2000) 2nd Ed.
    [2] https://pytorch.org/docs/stable/_modules/torch/optim/lbfgs.html
    [3] Jiang, Byrd, Eskow & Schnabel, "Preconditioned LBFGS", (2004)
    """
    def __init__(self, params, H0=None, lr=1.0, max_iter=20, max_ls_eval=10,
                 tolerance_grad=1e-10, tolerance_change=1e-12, line_search_fn=None):
        """
        Parameters
        ----------
        params : tuple
            Tuple of parameter tensors to optimize
        H0 : tensor
            Starting dense (N, N) inverse Hessian approximation.
            If not provided, default is to use y @ s / y @ y of first step.
        lr : float, optional
            Learning rate (default: 1)
        max_iter : int, optional
            Maximal number of iterations per optimization step
        max_ls_eval : int, optional
            Maximal number of function evaluations per line search
            (if line_search_fn is not None)
        tolerance_grad : float, optional
            termination tolerance on first order optimality
        tolerance_change : float, optional
            termination tolerance on function
            value/parameter changes
        line_search_fn : str, optional
            Either 'strong_wolfe' or None (just use lr)
        """
        self.lr = lr
        self.max_iter = max_iter
        self.max_ls_eval = max_ls_eval
        self.tolerance_grad = tolerance_grad
        self.tolerance_change = tolerance_change
        self.line_search_fn = line_search_fn
        self.params = list(params)
        self.func_evals = 0
        self.n_iter = 0
        self._loss = None
        self._flat_grad = None
        self._numel_cache = None
        self._alpha = None
        self._rho = None
        self._s, self._y = None, None
        self._exit = None

        if H0 is not None:
            if H0.ndim < 2:
                H0 = H0 * torch.eye(
                    self._numel(),
                    dtype=self.params[0].dtype,
                    device=self.params[0].device
                )
        self.H = H0

    def gather_flat_grad(self):
        views = []
        for p in self.params:
            if p.grad is None:
                view = p.new(p.numel()).zero_()
            elif p.grad.is_sparse:
                view = p.grad.to_dense().view(-1)
            else:
                view = p.grad.view(-1)
            views.append(view)
        return torch.cat(views, dim=0)

    def _numel(self):
        if self._numel_cache is None:
            self._numel_cache = sum([p.numel() for p in self.params])
        return self._numel_cache

    def update_params(self, step_size, update):
        """
        Move parameters in direction tensor "update" by scalar "step_size"
        """
        offset = 0
        for p in self.params:
            numel = p.numel()
            # view as to avoid deprecated pointwise semantics
            p.add_(update[offset:offset + numel].view_as(p), alpha=step_size)
            offset += numel
        assert offset == self._numel()

    def clone_param(self):
        return [p.clone(memory_format=torch.contiguous_format) for p in self.params]

    def set_param(self, params_data):
        """
        Overwrite params in self.params with params_data,
        which should be same shape and type as self.params
        (i.e. list of tensors)
        """
        for p, pdata in zip(self.params, params_data):
            p.copy_(pdata)

    def directional_evaluate(self, closure, x, alpha, p):
        """"
        Move params x along direction p by stepsize alpha,
        evaluate loss, then re-populate params as x
        """
        self.update_params(alpha, p)
        loss = float(closure())
        flat_grad = self.gather_flat_grad()
        self.set_param(x)
        return loss, flat_grad

    def update_hessian(self, s, y, alpha=None):
        """
        BFGS dense (inv.) hessian update to self.H

        Parameters
        ----------
        s : tensor
            Positional difference between steps k+1 and k
        y : tensor
            Gradient difference between steps k+1 and k
        alpha : float
            Stepsize for step k+1 <- k.
            Only provided to cache along with s, y, and rho
        """
        if self.H is None:
            # if H is not defined this is the first iteration
            # so we use Eqn 6.20 from [1]
            a = (y @ s) / (y @ y)
            self.H = a * torch.eye(self._numel(), dtype=s.dtype, device=s.device)

        rho = 1. / (y @ s)

        # cache these for debugging, generally of negligible size
        self._s, self._y, self._rho, self._alpha = s, y, rho, alpha

        # apply H update: requires sufficient curvature
        if (1. / rho) > 1e-10:
            # perform BFGS update
            # V = (I - rho y @ s^T)
            V = -rho * torch.outer(y, s)
            V.diagonal().add_(1.)

            # H update
            self.H = V.T @ self.H @ V

            # re-use allocated V for bias update
            torch.outer(rho * s, s, out=V)
            self.H += V

    def hvp(self, vec):
        """
        (Inv.) Hessian vector product

        Parameters
        ----------
        vec : tensor
            A vector with length equal to sidelength of Hessian

        Returns
        -------
        tensor
        """
        if self.H is None:
            # if self.H is not defined this is the first iteration
            # so we just use identity scaling for now
            return vec

        if self.H.ndim == 1:
            return self.H * vec
        else:
            return self.H @ vec

    @torch.no_grad()
    def step(self, closure):
        """
        Performs a single optimization step with a total of max_iter
        iterations.

        Parameteres
        -----------
        closure : callable
            A function that evaluates the model, computes gradients,
            and returns the (detached) loss.
        """
        self._exit = 0
        # Make sure the closure is always called with grad enabled
        closure = torch.enable_grad()(closure)

        # perform initial f(x) and df/dx if needed, or grab cache
        current_evals = 0
        if self._loss is None:
            loss = float(closure())
            current_evals += 1
            flat_grad = self.gather_flat_grad()
        else:
            loss, flat_grad = self._loss, self._flat_grad

        # evaluate optimum condition
        opt_cond = flat_grad.abs().max() <= self.tolerance_grad
        if opt_cond:
            self.exit = 2
            return loss

        # optimize for max_iter steps
        n_iter = 0
        while n_iter < self.max_iter:
            #########################
            # get step direction: p
            #########################
            p = -self.hvp(flat_grad)

            #############################
            # peform line search: alpha
            #############################
            # use self.lr as first-guess to alpha
            if self.n_iter == 0:
                alpha = float(min(1., 1. / flat_grad.abs().sum())) * self.lr
            else:
                alpha = self.lr

            # directional derivative
            gp = flat_grad @ p

            # check if directional derivative is below tolerance
            if gp > -self.tolerance_change:
                self._exit = 1
                break

            # now do the line search
            prev_loss = loss
            prev_flat_grad = flat_grad
            if self.line_search_fn is None:
                # no search function: just use fixed learning rate
                self.update_params(alpha, p)
                
                # now get new loss and grads at new position
                loss = float(closure())
                flat_grad = self.gather_flat_grad()
                opt_cond = flat_grad.abs().max() <= self.tolerance_grad
                ls_func_evals = 1

            elif self.line_search_fn == "strong_wolfe":
                # use strong wolfe conditions
                x = self.clone_param()
                def obj_func(x, alpha, p):
                    return self.directional_evaluate(closure, x, alpha, p)

                # get the proposed step_size alpha
                loss, flat_grad, alpha, ls_func_evals = strong_wolfe(
                    obj_func, x, alpha, p, loss, flat_grad, gp,
                    tolerance_change=self.tolerance_change, max_ls=self.max_ls_eval)
                alpha = float(alpha)

                # now update params
                self.update_params(alpha, p)
                opt_cond = flat_grad.abs().max() <= self.tolerance_grad

            else:
                raise NameError("didn't recognize line_search {}".format(self.line_search_fn))

            current_evals += ls_func_evals
            self.func_evals += ls_func_evals

            #############################
            # check break conditions
            #############################
            # optimal condition
            if opt_cond:
                self._exit = 2
                break

            # lack of progress
            if p.mul(alpha).abs().max() <= self.tolerance_change:
                self._exit = 3
                break
            if abs(loss - prev_loss) < self.tolerance_change:
                self._exit = 4
                break

            #############################
            # update inverse hessian
            #############################
            s = alpha * p
            y = flat_grad - prev_flat_grad
            self.update_hessian(s, y, alpha=alpha)

            n_iter += 1
            self.n_iter += 1

        self._loss = loss
        self._flat_grad = flat_grad

        return loss


class LBFGS(BFGS):
    """
    Limited memory BFGS algorithm from [1]. Uses the two-loop recursion
    to implicitly compute the (inv.) hessian vector product.

    [1] Nocedal & Wright, "Numerical Optimization", (2000) 2nd Ed.
    [2] https://pytorch.org/docs/stable/_modules/torch/optim/lbfgs.html
    [3] Jiang, Byrd, Eskow & Schnabel, "Preconditioned LBFGS", (2004)

    Notes:
        - no per-parameter options
        - all parameters must be on a single device
    """
    def __init__(self, params, H0=None, lr=1.0, max_iter=20, max_ls_eval=10,
                 history_size=100, tolerance_grad=1e-10, tolerance_change=1e-12,
                 line_search_fn=None, update_Hdiag=True):
        """
        Parameters
        ----------
        params : tuple
            Tuple of parameter tensors to optimize
        H0 : BaseMat subclass
            Starting inverse Hessian approximation. If not provided, default
            is to use y @ s / y @ y of most recent step in history.
            Note this is not the same as BFGS H0 parameter, which accepts
            a dense (N, N) tensor. Here, this should be a hesssian.DiagMat,
            hessian.SparseMat or hessian.PartitionedMat. Note this is
            saved as self.H.
        lr : float, optional
            Learning rate (default: 1)
        max_iter : int, optional
            Maximal number of iterations per optimization step
        max_ls_eval : int, optional
            Maximal number of function evaluations per line search
            (if line_search_fn is not None)
        history_size : int, optional
            Number of previous steps to cache for limited memory.
        tolerance_grad : float, optional
            termination tolerance on first order optimality
        tolerance_change : float, optional
            termination tolerance on function
            value/parameter changes
        line_search_fn : str, optional
            Either 'strong_wolfe' or None (just use lr)
        update_Hdiag : bool, optional
            If True, multiply the starting Hessian with Eqn. 7.20 of [1]
            for every step.
        """
        assert not isinstance(H0, torch.Tensor), "H0 should be a hessian.BaseMat subclass"
        super().__init__(params, H0=H0, lr=lr, max_iter=max_iter,
                         max_ls_eval=max_ls_eval, tolerance_grad=tolerance_grad,
                         tolerance_change=tolerance_change, line_search_fn=line_search_fn)
        self.history_size = history_size
        # deque has O(1) popleft() and append(), but O(n) index, good for storing s, y
        self._s, self._y = deque(), deque()
        # rho and alpha will be float-lists, so performance isn't as critical
        self._rho, self._alpha = [], []
        self._Hdiag = None
        self.update_Hdiag = update_Hdiag

    def update_hessian(self, s, y, alpha=None):
        """
        LBFGS implicit (inv.) hessian update

        Parameters
        ----------
        s : tensor
            Positional difference between steps k+1 and k
        y : tensor
            Gradient difference between steps k+1 and k
        alpha : float
            Stepsize for step k+1 <- k.
            Only provided to cache along with s, y, and rho
        """
        # compute rho
        rho = 1 / (y @ s)
        self._alpha.append(alpha)

        # check if starting Hessian needs defining
        if self.H is None:
            # just use I
            self.H = hessian.DiagMat(self._numel(),
                                     torch.tensor([1.],
                                     device=self.params[0].device,
                                     dtype=self.params[0].dtype))

        # only update if sufficient curvature
        if (1. / rho) > 1e-10:
            # if memory limit reached, pop oldest entry
            if len(self._s) == self.history_size:
                self._s.popleft()
                self._y.popleft()
                self._rho = self.rho[1:]

            self._s.append(s)
            self._y.append(y)
            self._rho.append(rho)

            # check if we should update diagonal
            if self.update_Hdiag:
                new_Hdiag = 1. / (rho * (y @ y))
                prev_Hdiag = 1. if self._Hdiag is None else self._Hdiag
                self.H.scalar_mul(new_Hdiag / prev_Hdiag)
                self._Hdiag = new_Hdiag

    def hvp(self, vec):
        """
        Implicit (inv.) Hessian vector product

        Parameters
        ----------
        vec : tensor
            A vector with length equal to sidelength of Hessian

        Returns
        -------
        tensor
        """
        return two_loop_recursion(vec, self._s, self._y, self._rho, self.H)


def two_loop_recursion(vec, s, y, rho, H0=None):
    """
    Use the two-loop recursion to implicitly
    perform a matrix-vector product between
    the implicit matrix defined by s, y, rho
    and the vector vec.

    Parameters
    ----------
    vec : tensor
        Input vector to dot
    s : list
        List of position difference tensors
    y : list
        List of gradient difference tensors
    rho : list
        List of 1 / (s @ y) inner products
    H0 : tensor or *Mat object
        Starting matrix to dot into g
        in addition to the implicit
        representation. If feeding a
        tensor, it is assumed to have ndim = 1.

    Returns
    -------
    r : tensor
        Matrix dotted into vec
    """
    N = len(s)
    if H0 is None:
        H0 = torch.tensor(1., dtype=vec.dtype, device=vec.device)

    # first loop: iterate backwards from end of queue
    q = vec
    alpha = [None] * N
    for i in reversed(range(N)):
        alpha[i] = rho[i] * (s[i] @ q)
        q = q - alpha[i] * y[i]

    # dot q into starting H and then do second loop
    if isinstance(H0, torch.Tensor):
        if H0.ndim < 2:
            r = H0 * q
        else:
            r = H0 @ q
    elif isinstance(H0, hessian.BaseMat):
        r = H0(q)
    else:
        raise NameError

    # second loop
    for i in range(N):
        beta = rho[i] * (y[i] @ r)
        r = r + s[i] * (alpha[i] - beta)

    return r


def implicit_to_dense(H0, s, y):
    """
    Takes a history of position and gradient
    difference tensors and, with a starting inv.
    hessian, performs BFGS updates to construct
    an approximate, dense, inverse hessian

    Parameters
    ----------
    H0 : tensor
        Starting N X N inv. hessian tensor
    s : list
        List of positional difference tensors
    y : list
        List of gradient difference tensors

    Returns
    -------
    tensor
    """
    N = len(s[0])
    if isinstance(H0, hessian.BaseMat):
        H0 = H0.to_dense()
    elif H0 is None:
        H0 = torch.eye(N, dtype=s[0].dtype, device=s[0].device)
    elif isinstance(H0, torch.Tensor):
        if H0.ndim < 2:
            H0 = torch.atleast_1d(H0)
            if len(H0) == len(s[0]):
                H0 = torch.diag(H0)
            else:
                H0 = torch.eye(N, dtype=s[0].dtype, device=s[0].device) * H0

    # setup BFGS class object w/ dummy parameters
    B = BFGS((s[0],), H0=H0)

    # iterate over s, y pairs
    for s_k, y_k in zip(s, y):
        B.update_hessian(s_k, y_k)

    return B.H


def cubic_interpolate(x1, f1, g1, x2, f2, g2, bounds=None):
    """
    This is copied from PyTorch(v2.0)'s torch.optim.lbfgs.py,
    but we add parameter documentation here.

    Parameters
    ----------
    x1 : float
        Starting parameter value
    f1 : float
        Function value at x1
    gp1 : float
        Directional gradient at x1
    x2 : float
        Ending parameter value
    f2 : float
        Function value at x2
    gp2 : float
        Directional gradient at x2
    bounds : tuple
        2-tuple containing (left, right) boundaries

    Returns
    -------
    float
    """
    # ported from https://github.com/torch/optim/blob/master/polyinterp.lua
    # Compute bounds of interpolation area
    if bounds is not None:
        xmin_bound, xmax_bound = bounds
    else:
        xmin_bound, xmax_bound = (x1, x2) if x1 <= x2 else (x2, x1)

    # Code for most common case: cubic interpolation of 2 points
    #   w/ function and derivative values for both
    # Solution in this case (where x2 is the farthest point):
    #   d1 = g1 + g2 - 3*(f1-f2)/(x1-x2);
    #   d2 = sqrt(d1^2 - g1*g2);
    #   min_pos = x2 - (x2 - x1)*((g2 + d2 - d1)/(g2 - g1 + 2*d2));
    #   t_new = min(max(min_pos,xmin_bound),xmax_bound);
    d1 = g1 + g2 - 3 * (f1 - f2) / (x1 - x2)
    d2_square = d1**2 - g1 * g2
    if d2_square >= 0:
        d2 = d2_square.sqrt()
        if x1 <= x2:
            min_pos = x2 - (x2 - x1) * ((g2 + d2 - d1) / (g2 - g1 + 2 * d2))
        else:
            min_pos = x1 - (x1 - x2) * ((g1 + d2 - d1) / (g1 - g2 + 2 * d2))
        return min(max(min_pos, xmin_bound), xmax_bound)
    else:
        return (xmin_bound + xmax_bound) / 2.


def strong_wolfe(obj_func, x, alpha, p, f, g, gp,
                 c1=1e-4, c2=0.9, tolerance_change=1e-9, max_ls=25):
    """
    This is copied from PyTorch(v2.0)'s torch.optim.lbfgs.py, but we add
    documentation and change parameter names to more closely match [1].

    Note that this accepts either tensors or ParamDict objects, in
    which case an independent line-search is run for each ParamDict key.

    [1] Nocedal & Wright, "Numerical Optimization", (2000) 2nd Ed.

    Parameters
    ----------
    obj_func : callable
        A function that takes (x0[tensor], alpha[float], p[tensor]),
        then evaluates and return loss, grad at x0 + alpha * p.
    x : tensor or ParamDict
        Starting flattened parameter tensor
    alpha : float or ParamDict
        Starting step-size to trial
    p : tensor or ParamDict
        Flattened search direction (i.e. H @ g)
    f : float
        Current function value (e.g. loss) at x0
    g : tensor or ParamDict
        Current (flattened) gradient at x0
    gp : tensor or ParamDict
        Directional gradient, or g @ p
    c1 : float, optional
        Armijo condition parameter [1] Eqn. 3.4
        with range (0, 1)
    c2 : float, optional
        Curvature condition parameter [1] Eqn 3.5
        with range (c1, 1)
    tolerance_change : float, optional
        Break condition if line-search alpha proposal
        is smaller than this
    max_ls : int, optional
        Maximum number of function evaluations for this
        line-search

    Returns
    -------
    f_new : float
        New function value at proposed step
    g_new : tensor or ParamDict
        New gradient value at proposed step
    alpha : float or ParamDict
        Proposed step-size
    ls_func_evals : int or dict
        Number of function evaluations during line-search
    """
    # first check if inputs are ParamDicts
    if isinstance(x, paramdict.ParamDict):
        assert isinstance(p, paramdict.ParamDict)
        assert isinstance(g, paramdict.ParamDict)
        assert isinstance(gp, paramdict.ParamDict)
        if not isinstance(alpha, (dict, paramdict.ParamDict)):
            alpha = {k: alpha for k in x}

        # now recursively call strong_wolfe for each key in x
        f_new, ls_func_evals = {}, {}
        g_new, alpha_new = paramdict.ParamDict({}), paramdict.ParamDict({})
        for k in x:
            out = strong_wolfe(obj_func, x[k], alpha[k], p[k], f, g[k], gp[k],
                               c1=c1, c2=c2, tolerance_change=tolerance_change, max_ls=max_ls)
            f_new[k], g_new[k], al_new, ls_func_evals[k] = out
            alpha_new[k] = torch.as_tensor(al_new, dtype=x[k].dtype, device=x[k].device)

        return f_new, g_new, alpha_new, ls_func_evals

    # ported from https://github.com/torch/optim/blob/master/lswolfe.lua
    p_norm = p.abs().max()
    g = g.clone(memory_format=torch.contiguous_format)

    # evaluate objective and gradient using initial step
    f_new, g_new = obj_func(x, alpha, p)
    ls_func_evals = 1
    gp_new = g_new.dot(p)

    # bracket an interval containing a point satisfying the Wolfe criteria
    alpha_prev, f_prev, g_prev, gp_prev = 0, f, g, gp
    done = False
    ls_iter = 0
    while ls_iter < max_ls:
        # check conditions
        if f_new > (f + c1 * alpha * gp) or (ls_iter > 1 and f_new >= f_prev):
            # armijo condition
            bracket = [alpha_prev, alpha]
            bracket_f = [f_prev, f_new]
            bracket_g = [g_prev, g_new.clone(memory_format=torch.contiguous_format)]
            bracket_gp = [gp_prev, gp_new]
            break

        if abs(gp_new) <= -c2 * gp:
            # curvature condition
            bracket = [alpha]
            bracket_f = [f_new]
            bracket_g = [g_new]
            done = True
            break

        if gp_new >= 0:
            # descent condition
            bracket = [alpha_prev, alpha]
            bracket_f = [f_prev, f_new]
            bracket_g = [g_prev, g_new.clone(memory_format=torch.contiguous_format)]
            bracket_gp = [gp_prev, gp_new]
            break

        # interpolate
        min_step = alpha + 0.01 * (alpha - alpha_prev)
        max_step = alpha * 10
        tmp = alpha
        alpha = cubic_interpolate(alpha_prev, f_prev, gp_prev, alpha, f_new, gp_new,
                                  bounds=(min_step, max_step))

        # next step
        alpha_prev = tmp
        f_prev = f_new
        g_prev = g_new.clone(memory_format=torch.contiguous_format)
        gp_prev = gp_new
        f_new, g_new = obj_func(x, alpha, p)
        ls_func_evals += 1
        gp_new = g_new.dot(p)
        ls_iter += 1

    # reached max number of iterations?
    if ls_iter == max_ls:
        bracket = [0, alpha]
        bracket_f = [f, f_new]
        bracket_g = [g, g_new]

    # zoom phase: we now have a point satisfying the criteria, or
    # a bracket around it. We refine the bracket until we find the
    # exact point satisfying the criteria
    insuf_progress = False

    # find high and low points in bracket
    low_pos, high_pos = (0, 1) if bracket_f[0] <= bracket_f[-1] else (1, 0)
    while not done and ls_iter < max_ls:
        # line-search bracket is too small
        if abs(bracket[1] - bracket[0]) * p_norm < tolerance_change:
            break

        # compute new trial value
        alpha = cubic_interpolate(bracket[0], bracket_f[0], bracket_gp[0],
                                  bracket[1], bracket_f[1], bracket_gp[1])

        # test that we are making sufficient progress:
        # in case `alpha` is so close to boundary, we mark that we are making
        # insufficient progress, and if
        #   + we have made insufficient progress in the last step, or
        #   + `alpha` is at one of the boundary,
        # we will move `alpha` to a position which is `0.1 * len(bracket)`
        # away from the nearest boundary point.
        eps = 0.1 * (max(bracket) - min(bracket))
        if min(max(bracket) - alpha, alpha - min(bracket)) < eps:
            # interpolation close to boundary
            if insuf_progress or alpha >= max(bracket) or alpha <= min(bracket):
                # evaluate at 0.1 away from boundary
                if abs(alpha - max(bracket)) < abs(alpha - min(bracket)):
                    alpha = max(bracket) - eps
                else:
                    alpha = min(bracket) + eps
                insuf_progress = False
            else:
                insuf_progress = True
        else:
            insuf_progress = False

        # Evaluate new point
        f_new, g_new = obj_func(x, alpha, p)
        ls_func_evals += 1
        gp_new = g_new.dot(p)
        ls_iter += 1

        if f_new > (f + c1 * alpha * gp) or f_new >= bracket_f[low_pos]:
            # Armijo condition not satisfied or not lower than lowest point
            bracket[high_pos] = alpha
            bracket_f[high_pos] = f_new
            bracket_g[high_pos] = g_new.clone(memory_format=torch.contiguous_format)
            bracket_gp[high_pos] = gp_new
            low_pos, high_pos = (0, 1) if bracket_f[0] <= bracket_f[1] else (1, 0)
        else:
            if abs(gp_new) <= -c2 * gp:
                # Wolfe conditions satisfied
                done = True
            elif gp_new * (bracket[high_pos] - bracket[low_pos]) >= 0:
                # old high becomes new low
                bracket[high_pos] = bracket[low_pos]
                bracket_f[high_pos] = bracket_f[low_pos]
                bracket_g[high_pos] = bracket_g[low_pos]
                bracket_gp[high_pos] = bracket_gp[low_pos]

            # new point becomes new low
            bracket[low_pos] = alpha
            bracket_f[low_pos] = f_new
            bracket_g[low_pos] = g_new.clone(memory_format=torch.contiguous_format)
            bracket_gp[low_pos] = gp_new

    # return stuff
    alpha = bracket[low_pos]
    f_new = bracket_f[low_pos]
    g_new = bracket_g[low_pos]

    return f_new, g_new, alpha, ls_func_evals

