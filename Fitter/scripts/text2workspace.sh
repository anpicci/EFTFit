#!/usr/bin/env bash

# Historical shell interface for the interference model. The Python builder
# owns command construction and guarded publication for this path.
make_workspace() {
    ulimit -s unlimited
    local script_directory
    script_directory=$(dirname -- "${BASH_SOURCE[0]}") || return 1
    python "${script_directory}/../python/workspace_build_recipe.py" --legacy-interference "$@"
}

make_workspace "$@"
