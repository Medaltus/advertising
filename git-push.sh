#!/bin/bash
# Safe push — always rebases on remote before pushing to avoid rejection
PAT="$1"
REMOTE="https://${PAT}@github.com/Medaltus/advertising.git"

echo "Fetching remote..."
git fetch "$REMOTE" main 2>&1 | grep -v "^$"

echo "Rebasing on origin/main..."
git rebase FETCH_HEAD

if [ $? -ne 0 ]; then
  echo "Rebase conflict — resolve conflicts, then run: git rebase --continue"
  exit 1
fi

echo "Pushing..."
git push "$REMOTE" main
