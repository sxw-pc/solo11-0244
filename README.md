# 命名空间租约与所有权交接台

离线模拟共享集群中的命名空间租期、团队责任交接和遗留资源清理。无需真实 Kubernetes、人员目录或计费服务；Python 标准库与 SQLite 可完成演示和测试。

`tenure` 维护命名空间 UID、租约代际、双角色续租、冻结及写入资格。`transfer` 固定资产清单，核旧新团队验收及费用、告警责任，托管后才可更换租约持有人。`cleanup` 记录资源、游离案、finalizer 和删除回执，资源未清不能关闭命名空间。`reappearance` 处理删除后重现的资源：观察只登记为带出来源的 sighting 并开重现案，案上固定退役实物 UID、内容摘要、存储位置与删除回执，代次更高不能推翻已签收的删除；独立存储端对固定 UID 回读取证（缺页、串 UID、内容被污染均不可恢复），客户与平台事前授权两种处置——`restore` 在冻结后把核验过的原实体交回原团队并开受限租约，`isolate` 保住删除事实、把复用名字的对象隔离到新 UID 的调查空间。归属未核实前重现案不继承旧团队写权，也不让命名空间绕过 finalizer 关闭。每次命令写入输入与结果事件，重放逐表校验。

```sh
python -m tenurecourt demo --db demo.sqlite
python -m tenurecourt replay --db demo.sqlite
python -m unittest discover -s tests -v
```

三个入口各有 `fixtures/<入口>_normal.json` 与 `_failure.json`。独立执行时按租约、清理资产登记、交接顺序共享一个新数据库；`demo` 已按此顺序运行全部六份样例。
