# 命名空间租约与所有权交接台

离线模拟共享集群中的命名空间租期、团队责任交接和遗留资源清理。无需真实 Kubernetes、人员目录或计费服务；Python 标准库与 SQLite 可完成演示和测试。

`tenure` 维护命名空间 UID、租约代际、双角色续租、冻结及写入资格。`transfer` 固定资产清单，核旧新团队验收及费用、告警责任，托管后才可更换租约持有人。`cleanup` 记录资源、游离案、finalizer 和删除回执，资源未清不能关闭命名空间。`incident_response` 把客户故障接报拆成值班席位、邮件入队、设备送达、本人确认与现场操作等独立证据，同一事故只开一张客户工单；修复写权仍由当时真实租约与冻结裁决，平台应急席位只能凭失联证据接管受限恢复命令，游离资源须独立平台权属证明。每次命令写入输入与结果事件，重放逐表校验。

```sh
python -m tenurecourt demo --db demo.sqlite
python -m tenurecourt replay --db demo.sqlite
python -m unittest discover -s tests -v
```

三个入口各有 `fixtures/<入口>_normal.json` 与 `_failure.json`。独立执行时按租约、清理资产登记、交接顺序共享一个新数据库；`demo` 已按此顺序运行全部六份样例。
