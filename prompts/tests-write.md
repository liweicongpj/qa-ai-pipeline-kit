任务：按确认后的 autotest/用例清单.md 写 pytest 用例（被测服务：{{target}}）。

1. 用例放在 {{tests_dir}}/test_*.py，用 pytest + httpx
2. 服务地址用 autotest/conftest.py 提供的 base_url 夹具，不要写死
3. 每个用例自己准备数据、互不依赖；接口文档有重置或造数据的办法就用
4. 涉及金币、物品、次数、订单的：执行前后拍快照，写对账断言（变化量 = 这次操作应有的产出减消耗；重复提交只入账一次）
5. 断言只用确定的计算，不要“大致正确”；时间相关的用接口文档里的调时钟办法，不要真的 sleep 等
6. 不变量写进 autotest/invariants.py：定义 check(base_url) -> list[str]，查不变量，返回违反的描述（空列表 = 没问题）。框架的 conftest.py 会在每个用例结束后自动调用；个别用例确实不适用时加 @pytest.mark.no_invariants
7. 写完运行 python -m pytest {{tests_dir}} --collect-only -q 确认能收集到
8. 停下来：告诉用户写了哪些用例和不变量，请用户看断言；然后运行 {{qa}} autotest run{{target_opt}}（服务没在跑可加 --start）
