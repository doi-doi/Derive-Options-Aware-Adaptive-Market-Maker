.PHONY: shadow shadow-48h shadow-baseline shadow-baseline-48h

shadow:
	PYTHONPATH=src:. python -m condor.shadow --profile configs/shadow_competition_800_usdc.yml --duration 15m

shadow-48h:
	PYTHONPATH=src:. python -m condor.shadow --profile configs/shadow_competition_800_usdc.yml --duration 48h

shadow-baseline:
	PYTHONPATH=src:. python -m condor.shadow_baseline --profile configs/shadow_competition_800_usdc.yml --duration 15m

shadow-baseline-48h:
	PYTHONPATH=src:. python -m condor.shadow_baseline --profile configs/shadow_competition_800_usdc.yml --duration 48h
