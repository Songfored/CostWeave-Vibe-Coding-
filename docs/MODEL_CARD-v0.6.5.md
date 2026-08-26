# v0.6.5 model card

CostWeave v0.6.5 is a control-plane contract, not a trained language model.
It assigns outcome credit to six orchestration layers and evaluates logged
policy candidates offline.

## Intended use

- audit why an accepted or rejected task received its outcome;
- compare a candidate policy with a champion using IPS or DR;
- identify missing evidence, selection bias and rollback requirements;
- prepare a shadow experiment without changing production routing.

## Not intended for

- automatic model training;
- automatic promotion or live route mutation;
- claims about real savings or provider quality from E0 fixtures;
- replacing human review for high-risk tasks.

## Known limitations

Credit is local attribution, not causal proof. Counterfactual replay is valid
only under its recorded assumptions. IPS and DR are invalid without positive
logging propensity and common support. Delayed labels, confounding, drift and
unmeasured Validator correlation remain open risks.
