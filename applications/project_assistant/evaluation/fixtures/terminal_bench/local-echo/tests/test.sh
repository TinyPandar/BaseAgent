#!/bin/bash
# Runs in the agent's container after the agent finishes. Reward 1 only for the exact answer.
mkdir -p /logs/verifier
if [ -f /app/answer.txt ] && [ "$(cat /app/answer.txt)" = "ok" ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
