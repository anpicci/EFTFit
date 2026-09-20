#!/usr/bin/env bash

# Create the interference-model workspace without silently replacing an existing file.
# Model arguments are intentionally unchanged; only the output lifecycle is wrapped.

make_workspace() {
    local overwrite=no
    local workspace_name=workspace.root
    local text2workspace_executable=text2workspace.py
    local combined_card=combinedcard.txt
    local scaling_data=scalings.json
    local physics_model=HiggsAnalysis.CombinedLimit.InterferenceModels:interferenceModel
    local argument

    while [ "$#" -gt 0 ]; do
        argument=$1
        case "$argument" in
            --overwrite)
                overwrite=yes
                shift
                ;;
            --output)
                if [ "$#" -lt 2 ]; then
                    printf '%s\n' '--output requires a path' >&2
                    return 2
                fi
                workspace_name=$2
                shift 2
                ;;
            --executable)
                if [ "$#" -lt 2 ]; then
                    printf '%s\n' '--executable requires a path' >&2
                    return 2
                fi
                text2workspace_executable=$2
                shift 2
                ;;
            --help)
                printf '%s\n' 'usage: source text2workspace.sh [--output PATH] [--overwrite] [--executable PATH]'
                return 0
                ;;
            *)
                printf 'Unknown argument: %s\n' "$argument" >&2
                return 2
                ;;
        esac
    done

    ulimit -s unlimited

    local target_directory
    local target_basename
    local temporary_directory
    local temporary_workspace
    target_directory=$(dirname -- "$workspace_name") || return 1
    target_basename=$(basename -- "$workspace_name") || return 1
    if [ ! -d "$target_directory" ]; then
        printf 'Workspace target directory does not exist: %s\n' "$target_directory" >&2
        return 1
    fi

    if [ -e "$workspace_name" ] || [ -L "$workspace_name" ]; then
        if [ ! -f "$workspace_name" ] || [ -L "$workspace_name" ]; then
            printf 'Workspace target is not a regular file: %s\n' "$workspace_name" >&2
            return 1
        fi
        if [ "$overwrite" != yes ]; then
            printf 'Workspace target already exists; pass --overwrite to replace it: %s\n' "$workspace_name" >&2
            return 1
        fi
        printf 'Replacing workspace resolved_path=%s size_bytes=%s sha256=%s\n' \
            "$(readlink -f -- "$workspace_name")" \
            "$(stat -c %s -- "$workspace_name")" \
            "$(sha256sum -- "$workspace_name" | awk '{print $1}')"
    fi

    temporary_directory=$(mktemp -d "${target_directory}/.${target_basename}.tmp.XXXXXX") || return 1
    temporary_workspace="${temporary_directory}/${target_basename}"

    printf '\nGenerating a temporary workspace before publication:\n'
    printf '%s %s -P %s --PO scalingData=%s --PO verbose -o %s\n\n' \
        "$text2workspace_executable" "$combined_card" "$physics_model" \
        "$scaling_data" "$temporary_workspace"

    if ! time "$text2workspace_executable" "$combined_card" \
        -P "$physics_model" \
        --PO "scalingData=$scaling_data" \
        --PO verbose \
        -o "$temporary_workspace"; then
        rm -f -- "$temporary_workspace"
        rmdir -- "$temporary_directory"
        return 1
    fi

    if [ ! -s "$temporary_workspace" ]; then
        printf 'Generated workspace is missing or empty; preserving existing target.\n' >&2
        rm -f -- "$temporary_workspace"
        rmdir -- "$temporary_directory"
        return 1
    fi
    if [ "$overwrite" = yes ]; then
        if ! mv -f -- "$temporary_workspace" "$workspace_name"; then
            rm -f -- "$temporary_workspace"
            rmdir -- "$temporary_directory"
            return 1
        fi
    else
        if ! ln -- "$temporary_workspace" "$workspace_name"; then
            printf 'Workspace target appeared during generation; preserving it.\n' >&2
            rm -f -- "$temporary_workspace"
            rmdir -- "$temporary_directory"
            return 1
        fi
        rm -f -- "$temporary_workspace"
    fi
    rmdir -- "$temporary_directory" || return 1

    printf 'Published workspace: %s\n' "$workspace_name"
}

make_workspace "$@"
