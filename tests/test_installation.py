from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agentic_kernel.installation import installation_id


def test_concurrent_installation_identity_has_one_winner(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=8) as executor:
        values = list(executor.map(lambda _: installation_id(tmp_path), range(32)))

    assert len(set(values)) == 1
    assert (tmp_path / "installation.json").read_text(encoding="utf-8") == values[0]
