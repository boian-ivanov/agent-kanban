#!/usr/bin/env bash
#
# lane-release.sh — release a worktree lane after its card is merged.
#
# Lane lifecycle (see examples/task-driver.py, "Worktree lanes"):
#   allocate  the driver runs `git worktree add -b task/<id> <root>/<n> <base>`
#             under fcntl.flock; branch `task/<id>` IS the lane identity and
#             `git worktree list --porcelain` is the ONLY ledger (no registry).
#   work      the agent edits in its lane; the board owner merges the branch.
#   release   THIS SCRIPT, invoked by the orchestrator AFTER the merge: drop
#             the worktree and delete the branch. It refuses, before touching
#             anything, when that would lose work — uncommitted/untracked
#             changes in the lane, or commits the base branch never saw.
#
# Usage:
#   lane-release.sh <project_id> --check
#       List every lane under the lane root, one line each:
#         lane path=<p> branch=<b> task=<id> exists=<true|false> \
#              dirty=<true|false> unmerged=<true|false|unknown>
#       Informational only: never mutates anything, always exits 0.
#   lane-release.sh <project_id> <task_id> [--force]
#       `git worktree remove <path>` then `git branch -D task/<task_id>`.
#       Refuses (exit 3) when the lane is dirty or the branch is unmerged;
#       --force skips both checks and DISCARDS that work permanently.
#
# Exit codes: 0 ok · 1 environment/git failure · 2 usage · 3 unsafe (dirty or
# unmerged, needs --force) · 4 no worktree on that branch.
#
# Repo + lane config resolution, in order:
#   1. env: LANE_PROJECT_PATH (main checkout), LANE_CONFIG (JSON file
#      {<project_id>: {enabled, root, base_branch, count, setup[]}})
#   2. board: GET $KANBAN_URL/api/board?project=<id> -> .project.path and
#      .project.worktrees
#   3. kanban_data/worktrees.json in this repo, keyed by project id
# This script only ever reads the board (GET); it never comments, never moves
# a card, never POSTs. It never runs `git worktree prune` and never touches a
# worktree sitting on another branch.

set -euo pipefail

KANBAN_URL="${KANBAN_URL:-http://127.0.0.1:7777}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKTREES_JSON_DEFAULT="$REPO_ROOT/kanban_data/worktrees.json"

usage() {  # the header IS the help text — no second copy to drift
    sed -n '/^# Usage:/,/^# Exit codes/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

die() {  # die <message...> — hard failure (exit 1)
    printf 'lane-release: %s\n' "$*" >&2
    exit 1
}

command -v git >/dev/null 2>&1 || die "git not found"

project_id="${1:-}"
[ -n "$project_id" ] || usage
shift

mode=release
task_id=""
case "${1:-}" in
    --check)
        mode=check
        shift
        ;;
    "" | --*)
        usage
        ;;
    *)
        task_id="$1"
        shift
        ;;
esac
force=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --force) force=1 ;;
        *) usage ;;
    esac
    shift
done
if [ "$mode" = release ] && [ -z "$task_id" ]; then
    usage
fi
# --check is informational and must never fail the caller.
fail_check() {
    printf 'lane-release: %s\n' "$*" >&2
    exit 0
}

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

# --- JSON access ------------------------------------------------------------
# jq is the primary reader; the python fallback keeps the script usable where
# jq is absent (macOS does not ship it). Either backend returns "" instead of
# failing: a missing lane config must degrade to the shared-worktree defaults,
# not abort the release.
_HAVE_JQ=0
# Probe that jq actually runs: a shadowed/broken jq must not silently look
# like "no lane config" (which would drop the lane root to the default).
if command -v jq >/dev/null 2>&1 && jq -n . >/dev/null 2>&1; then
    _HAVE_JQ=1
fi
_PYTHON="${LANE_PYTHON:-$REPO_ROOT/.venv/bin/python}"
[ -x "$_PYTHON" ] || _PYTHON="$(command -v python3 || true)"

json_field() {  # json_field <file> <jq-filter using $pid> <python-expr using d,pid>
    local file="$1" filter="$2" expr="$3"
    [ -f "$file" ] || return 0
    if [ "$_HAVE_JQ" = 1 ]; then
        jq -r --arg pid "$project_id" "$filter" "$file" 2>/dev/null || true
    elif [ -n "$_PYTHON" ]; then
        "$_PYTHON" -c '
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(0)
pid = sys.argv[2]
try:
    v = eval(sys.argv[3], {"d": d, "pid": pid})
except Exception:
    sys.exit(0)
if v in (None, "", [], {}):
    sys.exit(0)
print(v if isinstance(v, str) else json.dumps(v))
' "$file" "$project_id" "$expr" 2>/dev/null || true
    fi
}

# --- repo + config resolution ----------------------------------------------
# The board is contacted only for the pieces the environment did not supply,
# so a test/lane run with LANE_PROJECT_PATH + LANE_CONFIG never talks HTTP.
board_json=""
if [ -z "${LANE_PROJECT_PATH:-}" ] || [ -z "${LANE_CONFIG:-}" ]; then
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 5 -G --data-urlencode "project=$project_id" \
            "$KANBAN_URL/api/board" >"$tmpdir/board.json" 2>/dev/null || true
        [ -s "$tmpdir/board.json" ] && board_json="$tmpdir/board.json"
    fi
fi

repo="${LANE_PROJECT_PATH:-}"
if [ -z "$repo" ] && [ -n "$board_json" ]; then
    repo="$(json_field "$board_json" '.project.path // empty' \
        '(d.get("project") or {}).get("path")')"
fi
repo="${repo/#\~/$HOME}"
if [ -z "$repo" ]; then
    if [ "$mode" = check ]; then
        fail_check "cannot resolve the checkout for project '$project_id'" \
            "(set LANE_PROJECT_PATH, or start the board at $KANBAN_URL)"
    fi
    die "cannot resolve the checkout for project '$project_id': set LANE_PROJECT_PATH, or make the board at $KANBAN_URL reachable ($WORKTREES_JSON_DEFAULT carries lane config only, never the checkout path)"
fi

cfg_json=""
if [ -n "${LANE_CONFIG:-}" ]; then
    [ -f "$LANE_CONFIG" ] || die "LANE_CONFIG not found: $LANE_CONFIG"
    cfg_json="$(json_field "$LANE_CONFIG" '.[$pid] // empty' 'd.get(pid)')"
else
    if [ -n "$board_json" ]; then
        cfg_json="$(json_field "$board_json" '.project.worktrees // empty' \
            '(d.get("project") or {}).get("worktrees")')"
    fi
    if [ -z "$cfg_json" ]; then
        cfg_json="$(json_field "$WORKTREES_JSON_DEFAULT" '.[$pid] // empty' 'd.get(pid)')"
    fi
fi
printf '%s' "$cfg_json" >"$tmpdir/cfg.json"

lane_root="$(json_field "$tmpdir/cfg.json" '.root // empty' '(d or {}).get("root")')"
lane_root="${lane_root:-$repo-lanes}"
lane_root="${lane_root/#\~/$HOME}"
# `enabled` is deliberately not consulted: the ledger is git, and a lane left
# behind by a project that later turned lane mode off still has to be released.
base_branch="$(json_field "$tmpdir/cfg.json" '.base_branch // empty' \
    '(d or {}).get("base_branch")')"
if [ -z "$base_branch" ]; then
    # Lanes are cut from the main checkout's branch (the driver's default base
    # is "master" only when no base is configured), so compare against the same
    # branch the orchestrator merges into, not a hardcoded name.
    base_branch="$(git -C "$repo" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    if [ -z "$base_branch" ] || [ "$base_branch" = HEAD ]; then
        base_branch="master"
    fi
fi

# --- lane helpers -----------------------------------------------------------
canon() {  # canon <path> — physical path, so /tmp vs /private/tmp never differ
    local out
    if out="$(cd "$1" 2>/dev/null && pwd -P)"; then
        printf '%s' "$out"
    else
        printf '%s' "$1"
    fi
}

# lane_entries: "<path>\t<branch>" per registered worktree ("-" = detached).
# `git worktree list --porcelain` is the ledger; it is read, never pruned.
lane_entries() {
    local line path="" branch="-"
    while IFS= read -r line; do
        case "$line" in
            "worktree "*) path="${line#worktree }" ;;
            "branch refs/heads/"*) branch="${line#branch refs/heads/}" ;;
            "")
                if [ -n "$path" ]; then
                    printf '%s\t%s\n' "$path" "$branch"
                fi
                path=""; branch="-"
                ;;
        esac
    done < <(git -C "$repo" worktree list --porcelain 2>/dev/null || true)
    [ -z "$path" ] || printf '%s\t%s\n' "$path" "$branch"
}

# commits_ahead <branch>: commits on the branch the base branch lacks.
# -1 = not computable (unknown branch or missing base ref) — never "merged".
commits_ahead() {
    local branch="$1" base_ref count
    base_ref="$(git -C "$repo" rev-parse --verify --quiet "refs/heads/$base_branch" || true)"
    [ -n "$base_ref" ] || { printf '%s' -1; return 0; }
    git -C "$repo" rev-parse --verify --quiet "refs/heads/$branch" >/dev/null || {
        printf '%s' -1
        return 0
    }
    count="$(git -C "$repo" rev-list --count "$base_branch..$branch" 2>/dev/null || true)"
    printf '%s' "${count:--1}"
}

is_dirty() {  # is_dirty <path> — any modified, staged or untracked file
    [ -d "$1" ] || return 1
    [ -n "$(git -C "$1" status --porcelain 2>/dev/null || true)" ]
}

if ! git -C "$repo" rev-parse --git-dir >/dev/null 2>&1; then
    if [ "$mode" = check ]; then
        fail_check "$repo is not a git repository"
    fi
    die "$repo is not a git repository"
fi

root_canon="$(canon "$lane_root")"

# --- --check ----------------------------------------------------------------
if [ "$mode" = check ]; then
    found=0
    while IFS=$'\t' read -r path branch; do
        [ -n "$path" ] || continue
        [ "$(canon "$(dirname "$path")")" = "$root_canon" ] || continue
        found=1
        task="-"
        case "$branch" in
            task/*) task="${branch#task/}" ;;
        esac
        exists=false
        [ -d "$path" ] && exists=true
        dirty=false
        is_dirty "$path" && dirty=true
        unmerged=unknown
        if [ "$branch" != "-" ]; then
            ahead="$(commits_ahead "$branch")"
            case "$ahead" in
                -1 | "") ;;
                0) unmerged=false ;;
                *) unmerged=true ;;
            esac
        fi
        printf 'lane path=%s branch=%s task=%s exists=%s dirty=%s unmerged=%s\n' \
            "$path" "$branch" "$task" "$exists" "$dirty" "$unmerged"
    done < <(lane_entries)
    if [ "$found" = 0 ]; then
        printf 'no lanes in %s\n' "$lane_root"
    fi
    exit 0
fi

# --- release ----------------------------------------------------------------
branch="task/$task_id"
lane_path=""
while IFS=$'\t' read -r path entry_branch; do
    if [ "$entry_branch" = "$branch" ]; then
        lane_path="$path"
        break
    fi
done < <(lane_entries)
if [ -z "$lane_path" ]; then
    printf 'lane-release: no worktree on branch %s (project %s, root %s)\n' \
        "$branch" "$project_id" "$lane_root" >&2
    exit 4
fi
# A checkout could sit on task/<id> itself (someone checked out the lane
# branch in the main tree). git would refuse both remove and branch -D, but
# the refusal should name the real problem: the main checkout is not a lane.
if [ "$(canon "$lane_path")" = "$(canon "$repo")" ]; then
    die "refusing to release $branch: it is checked out in the main worktree $repo, not in a lane under $lane_root"
fi

if [ "$force" = 0 ]; then
    problems=""
    if is_dirty "$lane_path"; then
        problems="${problems}dirty: $lane_path has uncommitted or untracked changes
"
    fi
    ahead="$(commits_ahead "$branch")"
    if [ "$ahead" = -1 ]; then
        problems="${problems}unmerged: cannot prove $branch is in base '$base_branch' (base ref not found)
"
    elif [ "$ahead" != 0 ]; then
        problems="${problems}unmerged: $branch has $ahead commit(s) not in '$base_branch'
"
    fi
    if [ -n "$problems" ]; then
        printf 'lane-release: refusing to release %s\n' "$branch" >&2
        printf '%s' "$problems" | while IFS= read -r p; do
            printf '  %s\n' "$p" >&2
        done
        printf '  re-run with --force to discard that work permanently (data loss)\n' >&2
        exit 3
    fi
fi

remove_args=(worktree remove)
if [ "$force" = 1 ]; then
    remove_args+=(--force)
fi
if ! git -C "$repo" "${remove_args[@]}" "$lane_path"; then
    die "git worktree remove failed for $lane_path"
fi
if ! git -C "$repo" branch -D "$branch"; then
    die "worktree $lane_path removed but branch delete failed for $branch"
fi
printf 'released lane %s branch=%s (worktree removed, branch deleted)\n' \
    "$lane_path" "$branch"
exit 0
