# ADR 0002: Contextual Thompson sampling with depth context and log-linear backoff priors

- Status: accepted
- Date: 2026-11-19

## Context

A waterfall calls providers in some order until one returns a verified email. The
order and the stopping point decide the cost. Provider quality varies by segment
(country x company size x seniority): an EU-focused vendor may be the best first
call for German SMB contacts and a waste of money for US enterprise contacts. The
router has to learn this online, from its own traffic, without a labelled training
set.

## Decision

1. **Arms = (segment, depth, provider)** with a Beta posterior over
   P(call yields a *verified* email). *Depth* (how many providers have already missed
   for this contact; buckets 0/1/2+) is part of the context because misses are
   correlated: someone three vendors cannot find is unlikely to be found by the
   fourth, so the stopping rule needs conditional rates, not marginal ones.
2. **Thompson sampling** for exploration: sample theta for each candidate, and call
   argmax theta / c (the optimal index for sequential search with known rates).
3. **Expected cost, not list price.** A second Beta per arm tracks P(any email
   returned). Then `c = price x (1 if charged on miss else P(found)) + P(found) x
   verify_cost`. Pay-per-hit vendors are much cheaper per call than their list price
   suggests. Before this change the simulated bandit consistently under-used one.
4. **Budget-aware stopping rule:** a provider is a candidate only if it fits in what
   is left of the per-contact cap and the global ledger budget, and only if its
   sampled net value theta x V - c is positive (V = value of a verified email). If no
   candidate is left, the waterfall stops.
5. **Log-linear backoff prior.** 45 segments x 3 depths x 4 providers = 540 arms, and
   rare segments (about 0.3% of traffic) would take tens of thousands of contacts to
   learn alone. Each arm's prior mean combines the provider's pooled rates along each
   dimension, additively in logit space (each one shrunk toward the global rate):
   `logit(m0) = sum_d logit(m_d) - (D - 1) logit(m_global)`, with pseudo-count
   `min(n_global, 20)`.
6. Catch-all outcomes count as "not verified" for learning: the call still cost money.
   Transport errors are *censored*: they say nothing about the vendor's data quality.

## Alternatives considered

- **Bayesian logistic regression per provider** (Laplace-approximated, Chapelle & Li
  2011): it generalises better and allows interactions, but needs matrix updates and
  is harder to explain and audit. The backoff prior gets most of the benefit with
  counts a RevOps person can read in a table.
- **UCB:** deterministic, so concurrent workers would all pick the same arm between
  updates. Thompson sampling randomises naturally across workers.
- **Segment-only context (`thompson-flat`)** is kept as an ablation.

## Consequences

- Regret is measured against a clairvoyant oracle: the best fixed sequence per
  segment under the true simulated parameters, found by enumerating all 65 ordered
  subsets of 4 providers. Because the waterfall only continues after a miss, any
  policy that conditions on (segment, providers tried) *is* such a sequence, so this
  oracle is the optimum of the policy class.
- Posteriors are stored in SQLite and reloaded at start-up, so learning survives
  deploys.
- Non-stationarity (vendor data quality drifting) is not yet handled. See the
  roadmap: discounted counts or a sliding window.
