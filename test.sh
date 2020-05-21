#!/bin/sh

args=${1-tests}
PYTHONPATH=src poetry run python -m pytest --cov-branch --cov=ansibulled $args -vv
