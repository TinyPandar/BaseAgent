"""Download the reviewed DeepSeek V4 tokenizer data; execute no archive code."""

import argparse
from hashlib import sha256
import io
from pathlib import Path
import urllib.request
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".baseagent/tokenizers/deepseek-v4.json"))
    args = parser.parse_args()
    with urllib.request.urlopen("https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip", timeout=30) as response:
        data = response.read(32_000_001)
    if sha256(data).hexdigest() != "e7310d1dafe0a86d8a5629fe78a7c763760f651db9b8682718a1781dcd6fe495":
        raise ValueError("official tokenizer archive changed; review it before updating the pinned checksum")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entry = archive.getinfo("deepseek_v4_tokenizer/tokenizer.json")
        if entry.file_size > 16_000_000:
            raise ValueError("tokenizer JSON exceeds 16 MB")
        blob = archive.read(entry)
    from baseagent.llm.estimation import TokenizerEstimator
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        if args.output.read_bytes() != blob:
            raise ValueError("output already exists with different contents")
    else:
        with args.output.open("xb") as stream:
            stream.write(blob)
    TokenizerEstimator(args.output)
    print(args.output.resolve())


if __name__ == "__main__":
    main()
