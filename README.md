# 实现改造补偿与联合资金分摊基础平台

本项目是一套可离线运行的 Python 服务端平台，供县、乡镇和村级工作人员管理新型城镇化安置、土地资源分配、危房安全勘察与改造复核。账号登录、角色权限、业务状态、幂等结果和审计事件保存在 SQLite 中，适合安置经办、自然资源、住建复核与审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/rural_allocation/`：乡镇片区、地块资源池、土地批次、家庭申请、分配运行与移交情景；
- `src/housing_safety/`：危房勘察协议、测量导入、异常复核、分析任务租约和安全结论；
- `src/remediation_review/`：改造案件、现场测量、风险分析、账号登录与质量审批；
- `src/joint_funding/`：危房改造联合资金分摊，资金批次、分摊规则版本、确认冻结、完工核销、释放结转与双人复核调整；
- `fixtures/`：离线验收使用的勘察协议和结构化测点；
- `tests/`：核心规则、权限、错误边界、事务、API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m rural_allocation.acceptance --workspace .
PYTHONPATH=src python3 -m housing_safety.acceptance --workspace .
PYTHONPATH=src python3 -m remediation_review.acceptance
PYTHONPATH=src python3 -m joint_funding.acceptance --workspace .
```

四条命令使用临时 SQLite 数据库完成村镇与地块登记、家庭申请分配、危房测量分析、改造审批和联合资金分摊，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m rural_allocation.api --database rural.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m housing_safety.api --database housing.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m remediation_review.api --database remediation.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m joint_funding.api --database funding.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON。账号登录和角色权限由服务端校验，进程重启后可以继续查询 SQLite 中的业务状态与审计历史。

## 联合资金分摊

`joint_funding` 把一笔危房改造支出拆成中央补助、省级配套、县级资金和家庭自筹四个来源，并保证每一分钱可解释：

- 资金批次登记来源、适用户别、改造范围、有效期、总额和每户封顶，可用余额 = 总额 − 冻结 − 已核销；
- 分摊规则按来源排序，修订产生新版本且不回写已确认项目，相同内容重复登记报冲突；
- 项目确认时按当时规则版本和批次余额注水式分摊，每行注明生效约束（每户封顶、批次余额或剩余需求），公共资金立即冻结；
- 完工核销按实际验收造价重算，单批次核销不超过其冻结额，超出封顶的部分自动转为家庭自筹；取消和失败释放全部冻结，部分完成的余量可释放回批次或结转到指定批次并登记转账台账；
- 核销编号幂等：相同内容重放返回原结果，相同编号不同内容报冲突；
- 人工调整须一名资金管理角色提议、另一名复核，通过后形成新的分摊版本并同步调整冻结额度；
- 家庭只能查看本户项目明细，资金管理角色查看批次与规则，审计角色读取哈希链审计事件，越权访问一律拒绝。
