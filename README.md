# 合箱现场试验与承运组合扩围

这是围绕合箱现场试验与承运组合扩围整理的本地合成数据分析工作区。数据仅为演练构造，不代表真实企业或检测记录。项目当前提供 Python 3.11+ 的基础命令入口和 JSON 合成资料；业务分析流程尚未包含。

## 运行

无需安装第三方依赖。设置 `PYTHONPATH=src` 后运行 `python3 -m parcel-trial-decision` 可查看工作区版本。运行现有基线检查：`PYTHONPATH=src python3 -m unittest discover -s tests -v`。

## 资料

`data/sample.json` 保存小型合成记录，顶层键分别为 assignments, parcels, claims, capacity。
