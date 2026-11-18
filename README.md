# 航运舱单拆并箱与重量守恒

给货代单证员使用。主提单与分提单拆并形成有向无环关系，件数和净毛重逐层守恒。

七个业务模块：舱单采信、包装映射、拆并编组、危险品检查、净毛重核验、报关对账、版本封存。

```sh
docker compose up --build -d
docker compose exec -T app pytest -q
```

访问 http://localhost:18241 。启动直接载入现场脏批。切换干净样例和旧编码样例可对照核验。干净批42个节点、6个主单，件数240、净重2400kg、毛重2640kg；现场脏批包含父级缺失、依赖环、危险品不相容及1kg毛重差异，封存状态为待处理。

各模块页面对应 `/<模块英文名>`，各模块执行入口为 `POST /api/<模块英文名>`，不传参即可独立演示。完整运行 `POST /api/runs` 支持 `dataset`（dirty/clean/legacy）、`csv_text` 与 `overrides` 参数。批次历史 `GET /api/runs`，详情 `GET /api/runs/{run_id}`。

同输入与规则版本生成相同批号，只保留一条批次。复核入口 `POST /api/runs/{run_id}/review` 提交当前 `revision`，陈旧提交409。归档包只有一个下载入口 `GET /api/export/{run_id}`，含证据和运行摘要。原始隔离在页面同屏显示。

SQLite使用命名卷，容器重启后保留历史。要重建演示可在Compose中改用新的卷名称，保留旧卷。

源码在 `app/`，样例在 `sample_data/`，测试在 `tests/`；定义见 `docs/DATA-DICTIONARY.md`，模块和边界测试对应关系见 `docs/DEPTH.md`。输入上限2MB，属于离线复核工具。

采信页支持上传或粘贴CSV。完整源字节与参数随批次持久化，跨页通过run_id读取同一结果。`POST /api/runs/{run_id}/rerun` 读取该批不可变输入快照重新计算，规则未变时批号不变。切换另一示例则创建或复用另一批次。

## 封存与恢复副本

存储故障后若只剩封存包与原采信快照，从 `GET /api/seals/{package_id}/bundle` 导出或 `POST /api/seals/import` 导入。导入时重算包标识与封签，任一不符即拒绝，不会覆盖现有封存件。

- `POST /api/runs/{run_id}/seal`：把可封存批次封成不可变包（`GET /api/seals`、`GET /api/seals/{package_id}`）。同一结果重复封存只保留一个包。
- `POST /api/recover/{package_id}`：用封存包里的原采信快照按现规则重建**恢复副本**，成功 200、失败 409。副本永远标注“非原封件”，不会回写旧封存件或当前批次。
- `GET /recover`：恢复台。同屏给出原件与副本的字段差异、逐单差异、来源（包标识/原规则版本/现规则版本/快照摘要）、封签复算结果，以及按“哪些业务结果仍能被证明”给出的裁决。
- `GET /api/recover/{copy_id}`、`GET /api/recover/{copy_id}/export`：回读副本 JSON 与可下载副本包（含 comparison/assessment）。

同一封存包只有一套副本记录：重试不会新增第二套可用副本；规则升级后重试在原记录上更新；重试失败也不会销毁此前可用的副本。

裁决按可证明性分档：封签复算不符或包标识不符 → 保留旧封存件，须回原规则环境重建；副本件重/封签已变 → 不能冒充原封件，受影响申报结果等原规则环境重建，副本仅作差异复核；件重与封签一致 → 优先恢复封存证据，无需等原规则环境。恢复失败时旧封存件与当前结果均保留。
