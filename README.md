# 任务2：养老规划 Agent 开发说明

请使用 Python 3 编写 `run.py`。判分时会使用 **Python 3.12** 运行考生程序。程序需要支持命令行调用，问题文本会作为第一个参数传入：

```bash
python3 run.py "客户V500001现在年龄多大？"
```

标准输出只能返回最终答案，不要输出 JSON、日志、解释或其他内容。例如：

```text
22岁
```

## 开发检查清单

提交前请确认：

- `run.py` 放在提交目录根目录。
- `python3 run.py "问题文本"` 能直接运行。
- 标准输出只打印最终答案，不打印调试日志。
- 数据库连接参数和表名从环境变量读取。
- 如果调用大模型，`ONE_API_URL` 必须从环境变量读取。
- `ONE_API_KEY` 从 题目描述 中提供的链接获取。
- `run.py` 支持被评测程序并行调用，不依赖全局可变状态。

## 提交文件与依赖安装

提交目录中必须包含 `run.py`。所有程序运行需要的文件都必须放在 `run.py` 所在目录下，包括代码文件、配置文件、模型文件、数据文件、依赖声明文件等。其他目录下的内容运行时无法加载，会导致判分失败。

`pymysql` 已在判题环境中预装，不需要通过 pip 额外安装；如果代码依赖其他 Python 包，请在 `run.py` 同级目录提供 `requirements.txt`，每行填写一个 pip 包名，例如：

```text
requests
```

提交后判分时会自动安装 `requirements.txt` 中声明的依赖。
如果没有该文件，则跳过依赖安装。
建议只声明确实需要的包，避免安装耗时过长或引入不稳定依赖。

## 并行评测要求

正式评测程序会并行调用同一个提交目录下的 `run.py`，即同一时间可能有多个进程执行：

```bash
python3 run.py "问题A"
python3 run.py "问题B"
python3 run.py "问题C"
```

因此你的程序必须支持并行调用。建议遵守以下要求：

- 不要在 `run.py` 运行过程中修改提交目录中的源码、配置文件或共享数据文件。
- 不要把中间结果写入固定文件名，例如 `tmp.json`、`result.txt`、`cache.db`，多个进程会互相覆盖。
- 如确实需要写临时文件，请使用唯一文件名，例如加入进程号、线程号、纳秒/毫秒时间戳、随机 UUID 等。
- 不要使用全局文件锁长时间串行化所有请求，否则会导致判题变慢甚至超时。
- 不要在本地维护“第几次调用”“上一题结果”等跨问题状态；每次执行都应只根据当前问题、数据库和必要配置独立作答。
- 日志请不要输出到标准输出；如需本地调试日志，请写入唯一日志文件，例如 `logs/run_{pid}_{time_ms}.log`。

最稳妥的方式是：每次 `run.py` 启动后只读取文件、查询数据库、按当前问题计算并打印最终答案，不修改共享文件。

## 数据库连接

训练集已经导入 MySQL。**所有连接参数（含表名）都通过环境变量传入，`run.py` 必须从环境变量读取，缺省值用作本地开发**。判题时会通过环境变量连接并使用实际测试集数据，你的源码不需要任何改动。

简单理解：

- 本地开发默认连接训练表：`train_base_table` / `train_action_table`。
- 正式判题会自动切换到测试表：`base_table` / `action_table`。
- 代码中不要写死表名，否则提交后可能仍然查询训练表，导致得分异常。

学生本地开发的默认值：

| 环境变量 | 本地开发默认值 | 说明 |
| --- | --- | --- |
| `TASK2_DB_HOST` | `172.16.48.27` | MySQL 主机 |
| `TASK2_DB_PORT` | `3306` | MySQL 端口 |
| `TASK2_DB_USER` | `test_user` | 数据库账号 |
| `TASK2_DB_PASSWORD` | `R6#pV9@kT3!xM2$q` | 数据库密码 |
| `TASK2_DB_NAME` | `cmb_contest` | 数据库名 |
| `TASK2_BASE_TABLE` | `train_base_table` | 客户基础信息表名 |
| `TASK2_ACTION_TABLE` | `train_action_table` | 客户行为信息表名 |

`run.py` 中必须按这种模式读取：

```python
import os
DB_HOST      = os.getenv("TASK2_DB_HOST", "172.16.48.27")
DB_PORT      = int(os.getenv("TASK2_DB_PORT", "3306"))
DB_USER      = os.getenv("TASK2_DB_USER", "test_user")
DB_PASSWORD  = os.getenv("TASK2_DB_PASSWORD", "R6#pV9@kT3!xM2$q")
DB_NAME      = os.getenv("TASK2_DB_NAME", "cmb_contest")
BASE_TABLE   = os.getenv("TASK2_BASE_TABLE", "train_base_table")
ACTION_TABLE = os.getenv("TASK2_ACTION_TABLE", "train_action_table")
```

SQL 中**不要硬编码表名**，请通过 f-string 或字符串拼接引用 `BASE_TABLE` / `ACTION_TABLE`：

```python
sql = f"SELECT * FROM {BASE_TABLE} WHERE User_ID=%s"
```

在程序中按需查询目标客户或聚合结果，严禁一次性把全量行为数据拉到本地。

## 数据表

本地开发可使用训练集表：

| 表名 | 说明 | 数据量 |
| --- | --- | --- |
| `train_base_table` | 训练集客户基础信息 | 3 条 |
| `train_action_table` | 训练集客户行为信息 | 133 条 |

判题时会使用实际测试集数据，测试集表结构和字段含义与训练集一致。

## 字段说明

客户基础信息表：

| 字段 | 说明 |
| --- | --- |
| `User_ID` | 客户标识，例如 `V500001` |
| `Age` | 年龄 |
| `Gender` | 性别 |
| `Rsk_Cd` | 风险评级 |
| `Net_Asset` | 净资产 |
| `Monthly_Income` | 月收入 |
| `Monthly_Expend` | 月支出 |
| `Pension` | 退休金（每月，预计值） |
| `Enterprise_Ann` | 企业年金（一次性提取，预计值） |

客户行为信息表：

| 字段 | 说明 |
| --- | --- |
| `user_id` | 客户标识 |
| `action_typ` | 行为类型，如浏览详情、浏览持仓、收藏、购买 |
| `prod_sub_typ` | 产品子类 |
| `prod_typ` | 产品大类 |
| `rsk_lvl` | 产品风险等级 |
| `acs_tm` | 行为时间 |

## 查询示例

使用 MySQL 客户端查询（学生开发环境只能看到训练表）：

```bash
mysql -h172.16.48.27 -P3306 -utest_user -p cmb_contest
```

```sql
SELECT Age
FROM train_base_table
WHERE User_ID = 'V500001';
```

在 `run.py` 中使用 Python 查询（连接参数和表名都从环境变量读取，本地自测时使用默认值就是训练表，提交后判题环境会自动使用实际测试集，**源码不需要改**）。同目录提供了完整示例文件 `db_query_example.py`：


## one-api 调用示例

对于开放式建议书、自然语言组织等问题，可以调用 one-api。确定性查询和计算题建议优先使用 SQL 与公式完成。

**重点：大模型请求地址 `ONE_API_URL` 必须从环境变量读取。** 判题环境会把 `ONE_API_URL` 替换为平台代理地址，并通过该地址统计每次调用的大模型 token 使用量。代码中不要硬编码 one-api 请求地址，否则平台无法统计 token 使用情况，从而影响最终成绩。

`ONE_API_KEY` 请从竞赛平台任务说明“提交说明”小节提供的链接获取，`ONE_API_MODEL` 从下方可用模型中选择。可按下方示例在代码中定义。

当前可用模型：

- `doubao-seed-2-0-lite-260428-cmb`
- `qwen3.6-flash`

建议配置方式：

| 配置项 | 建议写法 | 说明 |
| --- | --- | --- |
| `ONE_API_URL` | `os.getenv("ONE_API_URL", "https://one-api-other.nowcoder.com/v1/chat/completions")` | 必须这样写，判题时平台会替换为代理地址 |
| `ONE_API_KEY` | `"YOUR_ONE_API_KEY"` | 从任务说明“提交说明”小节提供的链接获取 |
| `ONE_API_MODEL` | `"qwen3.6-flash"` | 从上方可用模型列表选择 |

完整调用示例见同目录文件：`llm_call_example.py`。
