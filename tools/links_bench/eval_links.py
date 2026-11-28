"""对照标准答案评估框架认出的关联。用法：python tools/links_bench/eval_links.py Excel目录 [--show]
先用 gen_excel.py 造一套带标准答案的 Excel 配置。改了关联规则（qa_index.find_links）后重跑，看认出的条数和误报有没有变差。"""
import json
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 框架根目录
import qa_index  # noqa: E402

src = Path(sys.argv[1])
truth = json.loads((src / "truth.json").read_text(encoding="utf-8"))
with tempfile.TemporaryDirectory() as tmp:
    idx = qa_index.Index(Path(tmp) / "index.sqlite")
    t0 = time.time()
    qa_index.build(idx, [{"name": "配置", "kind": "config", "path": str(src)}], None)
    t1 = time.time()
    links = idx.links()
    t2 = time.time()
    ntab, nrow = idx.stats()["tables"], idx.stats()["table_rows"]
    idx.close()
print(f"建索引 {t1 - t0:.1f} 秒（{ntab} 张表、{nrow} 行），算关联 {t2 - t1:.2f} 秒；认出 {len(links)} 条（高 {sum(l['level'] == '高' for l in links)}）")

by = {}
for l in links:
    by.setdefault((l["from"], l["col"]), []).append(l)
res = Counter()
miss, wrong_target = [], []
for frm, col, to, tcol, how in truth:
    cands = by.get((frm, col), [])
    hit = next((l for l in cands if l["to"] == to), None)
    hard = how.startswith("hard_")
    if hit:
        top = cands[0]["to"] == to  # 排第一的是不是对的表
        res[("难" if hard else "可") + hit["level"] + ("" if top else "（不是第一候选）")] += 1
    else:
        res[("难" if hard else "可") + "没认出"] += 1
        miss.append((frm, col, to, tcol, how, [(c["to"], c["level"]) for c in cands]))
truth_keys = {(f, c, t) for f, c, t, _, _ in truth}
extra = [l for l in links if (l["from"], l["col"], l["to"]) not in truth_keys]
ok_cols = {(f, c) for f, c, _, _, _ in truth}
false_hi = [l for l in extra if l["level"] == "高" and not ((l["from"], l["col"]) in ok_cols and l is not by[(l["from"], l["col"])][0])]
print(f"标准答案 {len(truth)} 条（其中事先知道认不出的 {sum(h.startswith('hard_') for *_, h in truth)} 条）")
for k, v in sorted(res.items()):
    print(f"  {k}：{v}")
print(f"标准答案以外的：{len(extra)} 条，其中高 {sum(l['level'] == '高' for l in extra)} 条")
if "--show" in sys.argv:
    print("\n没认出的：")
    for m in miss:
        print("  ", m)
    print("\n标准答案以外的：")
    for l in extra:
        print(f"   [{l['level']}] {l['from']}.{l['col']} → {l['to']}.{l['key']}：{l['why']}")
    print("\n填错的编号有没有报出来：")
    for l in links:
        if l["missing"]:
            print(f"   [{l['level']}] {l['from']}.{l['col']} → {l['to']}：找不到 {l['missing']}")
