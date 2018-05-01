# Copyright (C) 2018  Leo Singer
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; either version 2 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
#
import numpy as np
import ptemcee.sampler
from tqdm import tqdm

__all__ = ('ez_emcee',)


def logp(x, lo, hi):
    return np.where(((x >= lo) & (x <= hi)).all(-1), 0.0, -np.inf)


class VectorLikePriorEvaluator(ptemcee.sampler.LikePriorEvaluator):

    def __call__(self, x):
        s = x.shape
        x = x.reshape((-1, x.shape[-1]))

        lp = self.logp(x, *self.logpargs, **self.logpkwargs)
        if np.any(np.isnan(lp)):
            raise ValueError('Prior function returned NaN.')

        ll = np.empty_like(lp)
        bad = (lp == -np.inf)
        ll[bad] = 0
        ll[~bad] = self.logl(x[~bad], *self.loglargs, **self.loglkwargs)
        if np.any(np.isnan(ll)):
            raise ValueError('Log likelihood function returned NaN.')

        return ll.reshape(s[:-1]), lp.reshape(s[:-1])


# Add support for a ``vectorize`` option, similar to the option provided by
# emcee >= 3.0.
class Sampler(ptemcee.sampler.Sampler):

    def __init__(self, nwalkers, dim, logl, logp,
                 ntemps=None, Tmax=None, betas=None,
                 threads=1, pool=None, a=2.0,
                 loglargs=[], logpargs=[],
                 loglkwargs={}, logpkwargs={},
                 adaptation_lag=10000, adaptation_time=100,
                 random=None, vectorize=False):
        super().__init__(nwalkers, dim, logl, logp,
                         ntemps=ntemps, Tmax=Tmax, betas=betas,
                         threads=threads, pool=pool, a=a, loglargs=loglargs,
                         logpargs=logpargs, loglkwargs=loglkwargs,
                         logpkwargs=logpkwargs, adaptation_lag=adaptation_lag,
                         adaptation_time=adaptation_time, random=random)
        self._vectorize = vectorize
        if vectorize:
            self._likeprior = VectorLikePriorEvaluator(logl, logp,
                                                       loglargs, logpargs,
                                                       loglkwargs, logpkwargs)

    def _evaluate(self, ps):
        if self._vectorize:
            return self._likeprior(ps)
        else:
            return super(self).evaluate(ps)


def ez_emcee(log_prob_fn, lo, hi, nindep=200,
             ntemps=10, nwalkers=None, nburnin=500,
             args=(), kwargs={}, **options):
    """Fire-and-forget MCMC sampling using `ptemcee.Sampler`, featuring
    automated convergence monitoring, progress tracking, and thinning.

    The parameters are bounded in the finite interval described by ``lo`` and
    ``hi`` (including ``-np.inf`` and ``np.inf`` for half-infinite or infinite
    domains).

    If run in an interactive terminal, live progress is shown including the
    current sample number, the total required number of samples, time elapsed
    and estimated time remaining, acceptance fraction, and autocorrelation
    length.

    Sampling terminates when all chains have accumulated the requested number
    of independent samples.

    Parameters
    ----------
    log_prob_fn : callable
        The log probability function. It should take as its argument the
        parameter vector as an of length ``ndim``, or if it is vectorized, a 2D
        array with ``ndim`` columns.
    lo : list, `numpy.ndarray`
        List of lower limits of parameters, of length ``ndim``.
    hi : list, `numpy.ndarray`
        List of upper limits of parameters, of length ``ndim``.
    nindep : int, optional
        Minimum number of independent samples.
    ntemps : int, optional
        Number of temperatures.
    nwalkers : int, optional
        Number of walkers. The default is 4 times the number of dimensions.
    nburnin : int, optional
        Number of samples to discard during burn-in phase.

    Returns
    -------
    chain : `numpy.ndarray`
        The thinned and flattened posterior sample chain,
        with at least ``nindep`` * ``nwalkers`` rows
        and exactly ``ndim`` columns.

    Other parameters
    ----------------
    kwargs :
        Extra keyword arguments for `ptemcee.Sampler`.
        *Tip:* Consider setting the `pool` or `vectorized` keyword arguments in
        order to speed up likelihood evaluations.

    Notes
    -----
    The autocorrelation length, which has a complexity of :math:`O(N \log N)`
    in the number of samples, is recalulated at geometrically progressing
    intervals so that its amortized complexity per sample is constant. (In
    simpler terms, as the chains grow longer and the autocorrelation length
    takes longer to compute, we update it less frequently so that it is never
    more expensive than sampling the chain in the first place.)
    """

    lo = np.asarray(lo)
    hi = np.asarray(hi)
    ndim = len(lo)

    if nwalkers is None:
        nwalkers = 4 * ndim

    nsteps = 64

    with tqdm(total=nburnin + nindep * nsteps) as progress:

        sampler = Sampler(nwalkers, ndim, log_prob_fn, logp,
                          ntemps=ntemps, loglargs=args, logpargs=[lo, hi],
                          **options)
        pos = np.random.uniform(lo, hi, (ntemps, nwalkers, ndim))

        # Burn in
        progress.set_description('Burning in')
        for pos, _, _ in sampler.sample(
                pos, iterations=nburnin, storechain=False, adapt=True):
            progress.update()

        acl = np.nan
        while not np.isfinite(acl) or sampler.time < nindep * acl:

            # Advance the chain
            progress.total = nburnin + max(sampler.time + nsteps,
                                           nindep * acl)
            progress.set_description('Sampling')
            for pos, _, _ in sampler.sample(pos, iterations=nsteps):
                progress.update()

            # Refresh convergence statistics
            progress.set_description('Checking convergence')
            acl = sampler.get_autocorr_time()[0].max()
            if np.isfinite(acl):
                acl = int(np.ceil(acl))
            accept = np.mean(sampler.acceptance_fraction[0])
            progress.set_postfix(acl=acl, accept=accept)

            # The autocorrelation time calculation has complexity N log N in
            # the number of posterior samples. Only refresh the autocorrelation
            # length estimate on logarithmically spaced samples so that the
            # amortized complexity per sample is constant.
            nsteps *= 2

    chain = sampler.chain[0, :, ::acl, :]
    s = chain.shape
    return chain.reshape((-1, s[-1]))
