# Synthetic real-model fixtures

Only `scripts/record_cassettes.py`'s fixed synthetic prompts belong here.
Run it with existing `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, and optionally
`DEEPSEEK_MODEL` environment variables (or local `.env`). It makes at most nine
billed requests and refuses overwrites. All three scenarios must pass recording,
privacy scanning, and offline replay before publication.

Three real DeepSeek fixtures are included: `deepseek_direct.jsonl` (one model
call, no tools), `deepseek_tool.jsonl` (two model calls, one tool), and
`deepseek_correction.jsonl` (three model calls, failed division followed by a
corrected call). Recording used six model requests in total. All three passed
privacy scanning with the supplied credential loaded only in the recording
process and replay with network connections blocked. No credential file was
copied into the repository. The separate fake-model tests use temporary files;
private recordings go in ignored `cassettes_local/`.
