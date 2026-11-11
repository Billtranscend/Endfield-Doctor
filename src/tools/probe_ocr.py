"""探针：对当前画面的简报列表区域做一次整块 OCR，打印每个文字框。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maa.resource import Resource  # noqa: E402
from maa.tasker import Tasker  # noqa: E402

from game import ROOT, connect, find_game_window, init_toolkit  # noqa: E402

LIST_ROI = [240, 95, 870, 545]


def main() -> int:
    init_toolkit()
    controller = connect(find_game_window())
    resource = Resource()
    resource.post_bundle(str(ROOT / "resource")).wait()
    tasker = Tasker()
    tasker.bind(resource, controller)

    override = {"Probe_OCR": {"recognition": "OCR", "roi": LIST_ROI, "action": "DoNothing", "timeout": 1, "rate_limit": 0}}
    detail = tasker.post_task("Probe_OCR", override).wait().get()
    reco = detail.nodes[0].recognition
    for r in sorted(reco.all_results, key=lambda r: (r.box[1], r.box[0])):
        print(f"{r.box} {r.score:.2f} {r.text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
