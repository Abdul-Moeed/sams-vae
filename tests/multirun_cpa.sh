#!/bin/bash

# Loop over 3 seeds: 0, 1, 2
for seed in {0..5}; do
    # Modify the YAML file using Python for each seed
    python -c "
import yaml

# Load the YAML file
path = '/data/a330d/projects/sams-vae/demo/cpa_lasry.yaml'
with open(path, 'r') as file:
    config = yaml.safe_load(file)

# Modify the seed value
config['seed'] = $seed

# Save the modified YAML file
with open(path, 'w') as file:
    yaml.safe_dump(config, file)
"

    # Run the Python script after modifying the seed
    python lasry.py

    echo "Finished running with seed $seed"

done