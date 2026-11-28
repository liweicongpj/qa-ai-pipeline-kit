任务：按确认后的用例清单写 pytest 用例（被测服务：{{target}}）。

{{api_note}}

1. 用例放在 {{tests_dir}}/test_*.py，用 pytest + httpx；每个用例的文档字符串（docstring）里写上它对应的最终测试点编号，格式“R002-T03”（一个用例对应几条就写几个）
   - 清单里标“等接口”的用例，先写好数据准备和断言思路，加 @pytest.mark.skip(reason="等接口")
2. 服务地址用 autotest/conftest.py 提供的 base_url 夹具，不要写死
3. 每个用例自己准备数据、互不依赖；接口文档有重置或造数据的办法就用
4. 涉及金币、物品、次数、订单的：执行前后拍快照，写对账断言（变化量 = 这次操作应有的产出减消耗；重复提交只入账一次）
5. 断言只用确定的计算，不要“大致正确”；时间相关的用接口文档里的调时钟办法，不要真的 sleep 等
6. 不变量写进 autotest/invariants.py：定义 check(base_url) -> list[str]，查不变量，返回违反的描述（空列表 = 没问题）；个别用例确实不适用时加 @pytest.mark.no_invariants
7. 已有的用例文件要改的，输出整个文件；框架写完会用 pytest 收集一遍，收集失败会把报错发回给你

# 资料

{{context}}
