#!/bin/bash
# Script to collapse history from divergence point (b00d5ae) to convergence point (5f2368e)
# This will squash all commits in the divergence period into a single commit

set -e

DIVERGENCE_POINT="b00d5ae"  # Where we diverged from cpanel/main
CONVERGENCE_POINT="5f2368e"  # Where we merged cpanel state back in
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)

echo "=== Collapsing History from Divergence to Convergence ==="
echo ""
echo "Current branch: $CURRENT_BRANCH"
echo "Divergence point: $DIVERGENCE_POINT (where we diverged from cpanel/main)"
echo "Convergence point: $CONVERGENCE_POINT (where cpanel state was merged in)"
echo ""

# Safety check
if [ "$CURRENT_BRANCH" = "main" ]; then
    echo "WARNING: You are on the main branch!"
    echo "This script will create a new branch for safety."
    read -p "Continue? (yes/no): " confirm
    if [ "$confirm" != "yes" ]; then
        echo "Aborted."
        exit 1
    fi
fi

# Count commits to squash
COMMITS_TO_SQUASH=$(git rev-list --count ${DIVERGENCE_POINT}..${CONVERGENCE_POINT})
COMMITS_AFTER=$(git rev-list --count ${CONVERGENCE_POINT}..HEAD)

echo "Commits to squash (divergence period): $COMMITS_TO_SQUASH"
echo "Commits to keep after convergence point: $COMMITS_AFTER"
echo ""

# Create backup branch
BACKUP_BRANCH="main-backup-$(date +%Y%m%d-%H%M%S)"
echo "Creating backup branch: $BACKUP_BRANCH"
git branch "$BACKUP_BRANCH" "$CURRENT_BRANCH"
echo "✓ Backup created"
echo ""

# Create working branch
WORK_BRANCH="collapse-divergence"
if git show-ref --verify --quiet refs/heads/$WORK_BRANCH; then
    echo "Branch $WORK_BRANCH already exists. Deleting it..."
    git branch -D "$WORK_BRANCH"
fi

echo "Creating working branch: $WORK_BRANCH"
git checkout -b "$WORK_BRANCH" "$CURRENT_BRANCH"
echo "✓ Working branch created"
echo ""

echo "=== Step 1: Reset to divergence point ==="
echo "Resetting to divergence point (keeping changes)..."
git reset --soft "$DIVERGENCE_POINT"
echo "✓ Reset complete"
echo ""

echo "=== Step 2: Create a single commit with all divergence period changes ==="
echo "Checking out the state at convergence point ${CONVERGENCE_POINT:0:7}..."
git checkout "$CONVERGENCE_POINT" -- .
echo "✓ Files from convergence point checked out"

# Remove any files that shouldn't be there
git rm -f messages/docstring.files messages/marked.files configure.in 2>/dev/null || true

# Stage all changes
git add -A

# Get a summary of what changed
CHANGED_FILES=$(git diff --cached --name-only 2>/dev/null | wc -l | tr -d ' ')
if [ -z "$CHANGED_FILES" ] || [ "$CHANGED_FILES" = "0" ]; then
    CHANGED_FILES=$(git status --short | wc -l | tr -d ' ')
fi
echo "Files changed: $CHANGED_FILES"

# Create commit message
COMMIT_MSG="Squashed divergence period: Python 3 migration and fixes

This commit combines all work done during the divergence period from
cpanel/main, from the divergence point (${DIVERGENCE_POINT:0:7}) through
the convergence point where cpanel fixes were merged in (${CONVERGENCE_POINT:0:7}).

Includes:
- Python 2 to Python 3 migration work
- Pickle protocol handling fixes
- Encoding and string handling improvements
- Bug fixes and compatibility improvements
- Configuration and build system updates

Original commits: $COMMITS_TO_SQUASH commits from ${DIVERGENCE_POINT:0:7} to ${CONVERGENCE_POINT:0:7}"

echo ""
echo "Creating squashed commit..."
git commit -m "$COMMIT_MSG"
echo "✓ Squashed commit created"
echo ""

echo "=== Step 3: Re-apply commits after convergence point ==="
if [ "$COMMITS_AFTER" -gt 0 ]; then
    echo "Cherry-picking $COMMITS_AFTER commits after convergence point..."
    git cherry-pick ${CONVERGENCE_POINT}..${BACKUP_BRANCH}
    echo "✓ Commits re-applied"
else
    echo "No commits to re-apply after convergence point"
fi
echo ""

echo "=== Summary ==="
echo "✓ Backup branch created: $BACKUP_BRANCH"
echo "✓ Working branch created: $WORK_BRANCH"
echo "✓ Squashed $COMMITS_TO_SQUASH commits from divergence period into 1 commit"
if [ "$COMMITS_AFTER" -gt 0 ]; then
    echo "✓ Re-applied $COMMITS_AFTER commits after convergence point"
fi
echo ""
echo "Current branch: $WORK_BRANCH"
echo ""
echo "Next steps:"
echo "1. Review the changes: git log --oneline"
echo "2. Compare with original: git log --oneline $BACKUP_BRANCH | head -20"
echo "3. Test thoroughly"
echo "4. If satisfied, replace main:"
echo "   git checkout main"
echo "   git reset --hard $WORK_BRANCH"
echo "   git push origin main --force-with-lease"
echo ""
echo "To abort and restore:"
echo "   git checkout $CURRENT_BRANCH"
echo "   git branch -D $WORK_BRANCH"

