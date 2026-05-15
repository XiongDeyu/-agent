# run.py
import re
import sys
import os
import json
try:
    import pymysql
except ImportError:
    pymysql = None
import math
import urllib.request
import urllib.error
from datetime import date
from decimal import Decimal

# ------------------------------
# 数据库配置（环境变量读取）
# ------------------------------
DB_HOST = os.getenv("TASK2_DB_HOST", "172.16.48.27")
DB_PORT = int(os.getenv("TASK2_DB_PORT", "3306"))
DB_USER = os.getenv("TASK2_DB_USER", "test_user")
DB_PASSWORD = os.getenv("TASK2_DB_PASSWORD", "R6#pV9@kT3!xM2$q")
DB_NAME = os.getenv("TASK2_DB_NAME", "cmb_contest")
BASE_TABLE = os.getenv("TASK2_BASE_TABLE", "train_base_table")
ACTION_TABLE = os.getenv("TASK2_ACTION_TABLE", "train_action_table")

CURRENT_DATE = date(2025, 3, 31)

INFLATION_RATE = 0.02
INVEST_RATE = 0.02
EXPECTED_AGE = 80

ONE_API_URL = os.getenv("ONE_API_URL", "https://one-api-other.nowcoder.com/v1/chat/completions").strip()
ONE_API_KEY = os.getenv("ONE_API_KEY", "sk-mSnm0TPploSMetcZAb3dF5D286Ad46DfBdA73275F1Bb794a").strip()
ONE_API_MODEL = os.getenv("ONE_API_MODEL", "qwen3.6-flash").strip()

AGGREGATION_SQL_SYSTEM_PROMPT = """你是养老规划 Agent 的子技能：聚合查询 SQL 生成器。
你的任务：将用户提出的客户聚合统计问题转换为一条安全、可执行的 MySQL SELECT 查询语句。

必须严格遵守：
1) 你只负责生成 SQL，不直接回答最终数字。
2) 最终输出必须是 JSON，且只能输出 JSON，不得输出解释/Markdown/代码块。
3) 只能生成一条 SELECT；禁止 INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/TRUNCATE；禁止多语句。
4) 禁止 SELECT *。
5) 只能使用 BASE_TABLE 与 ACTION_TABLE；禁止使用其他表、禁止编造字段。
6) 聚合查询最终只能返回一个结果列，列名统一 result。
7) 禁止在 SQL 中写真实表名，必须使用占位符 BASE_TABLE、ACTION_TABLE。
8) 如无法确定问题含义，返回：
{
  "is_aggregation_query": false,
  "sql": "",
  "answer_type": "unknown",
  "unit": "无",
  "rounding": "none",
  "brief": "无法识别为聚合查询"
}

数据表：
- BASE_TABLE(User_ID, Age, Gender, Rsk_Cd, Net_Asset, Monthly_Income, Monthly_Expend, Pension, Enterprise_Ann)
- ACTION_TABLE(user_id, action_typ, prod_sub_typ, prod_typ, rsk_lvl, acs_tm)

产品类别映射（严格按顺序匹配）：
1. 现金理财: prod_sub_typ='现金'
2. 定期存款: prod_sub_typ='一般性' AND prod_typ='存款'
3. 短债类产品: prod_typ IN ('理财','基金') AND rsk_lvl='R2'
4. 固收+产品: prod_typ IN ('理财','基金') AND rsk_lvl='R3'
5. 权益类产品: prod_typ='基金' AND rsk_lvl IN ('R4','R5')
6. 年金险: prod_sub_typ IN ('税延养老年金','养老年金')
7. 其他: 不满足以上
且 prod_typ='非财富' 不参与财富产品行为统计。

行为词映射：
- 浏览/看过/查看/浏览过 -> action_typ IN ('浏览详情','浏览持仓')
- 购买/买入/买过/已购买 -> action_typ='购买'
- 收藏/关注/加入收藏 -> action_typ='收藏'
- 仅说“行为”则不限制 action_typ

比较词映射：
- 在N次及以上/至少N次 -> HAVING COUNT(*) >= N
- 超过N次 -> HAVING COUNT(*) > N
- 大于/高于N -> > N
- 不少于/不低于N -> >= N
- 小于/低于N -> < N
- 不超过N -> <= N
- 等于N -> = N

输出 JSON 结构固定为：
{
  "is_aggregation_query": true|false,
  "sql": "SELECT ...",
  "answer_type": "count|average|sum|max|min|unknown",
  "unit": "个|岁|元|无",
  "rounding": "integer|none",
  "brief": "简短说明该 SQL 查询什么"
}
"""

AGGREGATION_FALLBACK = {
    "is_aggregation_query": False,
    "sql": "",
    "answer_type": "unknown",
    "unit": "无",
    "rounding": "none",
    "brief": "无法识别为聚合查询",
}

# ------------------------------
# LLM 问题类型识别器
# ------------------------------
LLM_CLASSIFIER_SYSTEM_PROMPT = """你是养老规划 Agent 的问题类型识别器。

你的任务：
根据用户问题判断标准问题类型，只输出一个类型字符串。
不要回答问题，不要解释，不要输出 JSON，不要输出 Markdown。

只能返回以下类型之一：
客户信息查询-年龄
客户信息查询-退休
客户行为偏好分析
客户购买预测
聚合查询
养老金缺口计算-月支出
养老金缺口计算-最低积累
养老金缺口计算-可积累
投资配置建议
综合建议书生成
未知

判断原则：

1. 如果用户要求生成建议书、规划报告、养老规划方案，返回：
综合建议书生成

2. 如果问题没有单个客户ID，并且出现“年龄/风险评级/净资产/月收入/月支出”等字段，同时询问“数量/人数/统计一下/多少个/多少人”，必须返回：
聚合查询

3. 如果问题询问某个客户当前年龄、几岁、多大，返回：
客户信息查询-年龄

4. 如果问题询问某个客户距离退休多久、什么时候退休、还要上几年班，返回：
客户信息查询-退休

5. 如果问题询问某个客户历史上更偏好什么产品、行为最多、最常浏览、最常购买、最感兴趣，返回：
客户行为偏好分析

6. 如果问题询问某个客户未来一周、下周、接下来最可能购买什么产品，返回：
客户购买预测
注意：必须出现“购买/买/会买/可能买/预测购买”等购买意图；仅出现“未来”不算客户购买预测。

7. 如果问题询问某个客户刚退休时、退休后为了保持消费水平，每月需要花多少钱，返回：
养老金缺口计算-月支出

8. 如果问题询问某个客户退休时最低需要积攒多少钱、至少准备多少钱、养老缺口是多少、要补多少钱，返回：
养老金缺口计算-最低积累

9. 如果问题询问某个客户到退休时能攒多少钱、可以积累多少钱、能剩下多少养老本金，返回：
养老金缺口计算-可积累

10. 如果问题询问如何投资、如何配置、定期存款能否达成目标、收益最大化、最小化风险、增加什么产品配置，返回：
投资配置建议

特殊规则：
- 有明确客户ID且问“多少钱/每月多少钱/最低攒多少钱/能攒多少钱”，通常不是聚合查询。
- “消费水平不下降”只是养老目标，不是问题类型。
- “如果/假设/未来”不是问题类型，继续看用户最终要做什么。
- 如果问题里同时有“生成建议书”和“资产配置”，优先返回综合建议书生成。
- “预期未来寿命延长到90岁，最可能增加什么产品配置”返回投资配置建议，不是客户购买预测。
- “浏览权益类产品2次以上的客户平均年龄”返回聚合查询。

只输出类型字符串。
"""

OTHER_ADVICE_SYSTEM_PROMPT = """你是养老规划 Agent 中的“其他建议生成 Skill”。

你的任务：
只生成养老规划建议书中的第 7 章“其他建议”。

你必须严格遵守：
1. 只输出第 7 章，不要输出其他章节。
2. 输出格式必须为：
7. 其他建议
• 建议1
• 建议2
• 建议3
（可输出 3 到 5 条建议）
3. 建议要从客户经理后续沟通视角出发，结合输入中的结构化事实。
4. 不要重新计算金额，不要修改已有测算结果，不要改动已有配置比例。
5. 不要编造输入中不存在的信息，不要推荐超出客户风险评级的产品。
6. 不要承诺收益，禁止使用“保证收益”“稳赚”“无风险”等表述。
7. 使用正式、清晰、可直接用于沟通的话术；每条建议 1 到 2 句话。
"""

VALID_QTYPES = {
    "客户信息查询-年龄",
    "客户信息查询-退休",
    "客户行为偏好分析",
    "客户购买预测",
    "聚合查询",
    "养老金缺口计算-月支出",
    "养老金缺口计算-最低积累",
    "养老金缺口计算-可积累",
    "投资配置建议",
    "综合建议书生成",
    "未知",
}


def normalize_llm_qtype(raw_text):
    text = (raw_text or "").strip()

    if not text:
        return "未知"

    # 去掉可能的 Markdown 代码块
    if text.startswith("```"):
        text = re.sub(r"^```(?:text|json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)

    text = text.strip()

    # 去掉常见前缀
    text = re.sub(r"^(问题类型|类型|qtype|标准类型)\s*[:：]\s*", "", text, flags=re.IGNORECASE)

    # 只取第一行，防止模型多说
    text = text.splitlines()[0].strip()

    # 精确命中
    if text in VALID_QTYPES:
        return text

    # 容错：如果模型输出里包含标准类型，取第一个匹配
    for qtype in VALID_QTYPES:
        if qtype != "未知" and qtype in text:
            return qtype

    return "未知"


def llm_classify_question(question):
    user_prompt = f"""请判断下面问题的标准问题类型，只输出标准类型字符串。

用户问题：
{question}
"""
    content = call_one_api_chat([
        {"role": "system", "content": LLM_CLASSIFIER_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ])
    return normalize_llm_qtype(content)


def should_use_llm_classifier(question, rule_qtype):
    q = (question or "").lower()
    user_id = extract_user_id(question or "")

    # 规则识别失败，必须调用
    if rule_qtype == "未知":
        return True

    # 有客户ID但被误识别成聚合查询，交给 LLM 纠正
    if user_id and rule_qtype == "聚合查询":
        return True

    # 仅有“未来”但没有购买含义，不应直接判购买预测
    if rule_qtype == "客户购买预测":
        has_buy_intent = any(k in q for k in [
            "买", "购买", "会买", "可能买", "最可能购买", "预测购买", "大概率会买"
        ])
        if not has_buy_intent:
            return True

        # 寿命/通胀这类“未来”不是购买预测
        if any(k in q for k in ["寿命", "通胀", "收益率", "退休年龄"]):
            return True

    # 投资配置建议和金额计算冲突时，让 LLM 决策
    if rule_qtype == "投资配置建议":
        money_calc_clues = [
            "刚退休", "每月需要", "每月支出", "一个月", "最低需要积攒",
            "最低需要攒", "最低积攒", "至少要攒", "可以积攒",
            "能攒下", "能积攒", "养老缺口", "要补多少钱"
        ]
        if any(k in q for k in money_calc_clues):
            return True

    # 年龄查询不能误伤平均年龄
    if rule_qtype == "客户信息查询-年龄" and "平均" in q:
        return True
    
     # 没有客户ID，却被识别为单客户信息查询，通常需要重新判断
    if rule_qtype in ["客户信息查询-年龄", "客户信息查询-退休"] and not extract_user_id(question or ""):
        return True

    return False


def get_question_type(question):
    rule_qtype = parse_question_type(question)

    if should_use_llm_classifier(question, rule_qtype):
        llm_qtype = llm_classify_question(question)

        # LLM 有明确结果时用 LLM；否则回退规则
        if llm_qtype != "未知":
            return llm_qtype

    return rule_qtype
# ------------------------------
# 产品库
# ------------------------------
PRODUCTS = [
    {"name": "现金理财", "risk_level": ["R1"], "expected_return": 0.015},
    {"name": "定期存款", "risk_level": ["R1"], "expected_return": 0.020},
    {"name": "短债类产品", "risk_level": ["R2"], "expected_return": [0.021, 0.027]},
    {"name": "固收+产品", "risk_level": ["R3"], "expected_return": [0.005, 0.080]},
    {"name": "权益类产品", "risk_level": ["R4", "R5"], "expected_return": [-0.040, 0.160]},
    {"name": "年金险", "risk_level": ["R1"], "expected_return": 0.025},
]

RISK_ORDER = {
    "R1": 1,
    "R2": 2,
    "R3": 3,
    "R4": 4,
    "R5": 5,
}


# ------------------------------
# 数据库连接
# ------------------------------
def get_connection():
    if pymysql is None:
        raise RuntimeError("pymysql is not installed")
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        db=DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor
    )


def query_customer_field(user_id, field):
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cursor:
            sql = f"SELECT {field} FROM {BASE_TABLE} WHERE User_ID=%s"
            cursor.execute(sql, (user_id,))
            row = cursor.fetchone()

            if row and field in row and row[field] is not None:
                value = row[field]

                if field in ("Rsk_Cd", "Gender"):
                    return str(value)

                if isinstance(value, Decimal):
                    return float(value)

                return value

    except Exception:
        pass

    finally:
        if conn:
            conn.close()

    return None


def normalize_aggregation_response(raw_text):
    text = (raw_text or "").strip()
    if not text:
        return dict(AGGREGATION_FALLBACK)

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)

    try:
        data = json.loads(text)
    except Exception:
        return dict(AGGREGATION_FALLBACK)

    if not isinstance(data, dict):
        return dict(AGGREGATION_FALLBACK)

    result = dict(AGGREGATION_FALLBACK)
    result.update({
        "is_aggregation_query": bool(data.get("is_aggregation_query", False)),
        "sql": str(data.get("sql", "") or ""),
        "answer_type": str(data.get("answer_type", "unknown") or "unknown"),
        "unit": str(data.get("unit", "无") or "无"),
        "rounding": str(data.get("rounding", "none") or "none"),
        "brief": str(data.get("brief", "无法识别为聚合查询") or "无法识别为聚合查询"),
    })

    sql_text = result["sql"].strip()
    lowered = sql_text.lower()
    if not result["is_aggregation_query"]:
        return dict(AGGREGATION_FALLBACK)

    banned = ("insert ", "update ", "delete ", "drop ", "alter ", "create ", "truncate ", ";")
    has_table_ref = ("base_table" in lowered) or ("action_table" in lowered)
    has_result_alias = re.search(r"\bresult\b", lowered) is not None

    if (
            not sql_text
            or not (lowered.startswith("select") or lowered.startswith("with"))
            or "select *" in lowered
            or any(b in lowered for b in banned)
            or (not has_table_ref)
            or (not has_result_alias)
    ):
        return dict(AGGREGATION_FALLBACK)

    return result


def call_one_api_chat(messages):
    if not ONE_API_URL:
        print("WARNING: ONE_API_URL 未配置，聚合查询 SQL 生成将返回降级结果", file=sys.stderr)
        return ""

    payload = {
        "model": ONE_API_MODEL,
        "messages": messages,
        "temperature": 0,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
    }
    if ONE_API_KEY:
        headers["Authorization"] = f"Bearer {ONE_API_KEY}"

    req = urllib.request.Request(ONE_API_URL, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"WARNING: one-api 请求失败: {e}", file=sys.stderr)
        return ""

    try:
        data = json.loads(raw)
    except Exception as e:
        print(f"WARNING: one-api 响应非 JSON: {e}", file=sys.stderr)
        return ""

    choices = data.get("choices") or []
    if not choices:
        return ""

    message = choices[0].get("message") or {}
    return str(message.get("content", "") or "")


def is_aggregation_query_candidate(question):
    q = (question or "").lower()
    agg_words = ["多少", "平均", "总和", "总计", "最大", "最小", "count", "avg", "sum", "max", "min"]
    behavior_words = ["浏览", "看过", "查看", "购买", "买入", "收藏", "关注", "行为", "次", "及以上", "至少", "超过"]
    scope_words = ["客户", "年龄", "风险评级", "风评等级", "净资产", "月收入", "月支出", "退休金", "企业年金", "产品"]
    return (any(w in q for w in agg_words) and any(w in q for w in scope_words)) or (
            any(w in q for w in behavior_words) and any(w in q for w in agg_words)
    )


def generate_aggregation_sql_json(question):
    user_prompt = f"用户问题：{question}"
    content = call_one_api_chat([
        {"role": "system", "content": AGGREGATION_SQL_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ])
    result = normalize_aggregation_response(content)
    return json.dumps(result, ensure_ascii=False)


def execute_aggregation_query(sql_template):
    sql = re.sub(r"\bBASE_TABLE\b", BASE_TABLE, sql_template, flags=re.IGNORECASE)
    sql = re.sub(r"\bACTION_TABLE\b", ACTION_TABLE, sql, flags=re.IGNORECASE)

    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cursor:
            cursor.execute(sql)
            row = cursor.fetchone()
            if not row:
                return None

            if isinstance(row, dict):
                if "result" in row:
                    return row["result"]
                for value in row.values():
                    return value
                return None

            if isinstance(row, (list, tuple)) and row:
                return row[0]

            return row
    except pymysql.MySQLError as e:
        print(f"WARNING: 聚合查询执行失败: {e}", file=sys.stderr)
        return None
    finally:
        if conn:
            conn.close()


def format_aggregation_answer(meta, value):
    if value is None:
        return "暂无数据"

    if isinstance(value, Decimal):
        value = float(value)

    answer_type = str(meta.get("answer_type", "unknown") or "unknown")
    rounding = str(meta.get("rounding", "none") or "none")
    unit = str(meta.get("unit", "无") or "无")

    try:
        numeric = float(value)
        if answer_type == "count" or rounding == "integer":
            value_text = str(int(round(numeric)))
        else:
            value_text = f"{numeric:.4f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        value_text = str(value)

    if unit != "无":
        return f"{value_text}{unit}"
    return value_text


# ------------------------------
# 提取客户ID
# ------------------------------
def extract_user_id(question):
    match = re.search(r"客户\s*([A-Za-z]\d{6})", question)
    if match:
        return match.group(1)

    match = re.search(r"(?<![A-Za-z0-9])([A-Za-z]\d{6})(?!\d)", question)
    if match:
        return match.group(1)

    return None


# ------------------------------
# 假设条件解析：只解析默认条件变化，不决定任务类型
# ------------------------------
def parse_amount_text(text):
    """
    将中文口语金额转成数字：
    10000元 -> 10000
    1万 -> 10000
    1万块 -> 10000
    1.5万 -> 15000
    """
    if text is None:
        return None

    text = str(text).strip()

    m = re.search(r"(\d+(?:\.\d+)?)\s*万", text)
    if m:
        return int(round(float(m.group(1)) * 10000))

    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块|块钱)?", text)
    if m:
        return int(round(float(m.group(1))))

    return None


def parse_scenario_overrides(question):
    q = (question or "").lower()
    scenario = {}

    has_scenario_word = any(k in q for k in ["如果", "假如", "假设", "若", "假定", "万一", "要是"])

    # 1. 预期寿命变化：默认 80 岁
    life_match = re.search(
        r"(?:预期寿命|人均寿命|寿命|活到|延长到|活到|能活到)[^\d]*(\d{2,3})\s*岁?",
        q
    )
    if life_match:
        new_life = int(life_match.group(1))
        if new_life != EXPECTED_AGE:
            scenario["expected_age"] = new_life

    # 2. 通胀率变化：默认 2%
    inflation_match = re.search(
        r"(?:通胀率|通胀)[^\d]*(\d+(?:\.\d+)?)\s*%",
        q
    )
    if inflation_match:
        new_inflation = float(inflation_match.group(1)) / 100
        if abs(new_inflation - INFLATION_RATE) > 1e-12:
            scenario["inflation_rate"] = new_inflation

            after_match = re.search(r"(\d+)\s*年后", q)
            if after_match:
                scenario["inflation_effective_after_years"] = int(after_match.group(1))

    # 3. 投资回报率变化：默认 2%
    # 覆盖：投资年化4%、年化能做到4%、默认收益率按4%、收益按4%、投资收益4%
    invest_match = re.search(
        r"(?:投资回报率|默认投资回报率|投资收益率|投资收益|收益率|默认收益率|年化收益率|年化收益|投资年化|年化)[^\d]*(\d+(?:\.\d+)?)\s*%",
        q
    )
    if invest_match:
        new_return = float(invest_match.group(1)) / 100
        if abs(new_return - INVEST_RATE) > 1e-12:
            scenario["invest_rate"] = new_return

    # 4. 指定退休年龄
    # 覆盖：假设65岁退休 / 干到65岁再退休 / 退休年龄改成65岁
    retirement_patterns = [
        r"(?:退休年龄|退休)[^\d]*(\d{2})\s*岁",
        r"(?:干到|工作到|做到|上班到)[^\d]*(\d{2})\s*岁.*?(?:退休|退)",
        r"(\d{2})\s*岁.*?(?:退休|退)"
    ]

    for pattern in retirement_patterns:
        retirement_match = re.search(pattern, q)
        if retirement_match and has_scenario_word:
            new_retire_age = int(retirement_match.group(1))

            # 避免把“活到90岁”误识别为退休年龄
            if 50 <= new_retire_age <= 70:
                scenario["retirement_age"] = new_retire_age
                break

    # 5. 指定退休后每月支出目标
    # 覆盖：退休后每月10000元 / 每个月花1万块 / 退休后想每个月花1万
    goal_patterns = [
        r"退休后.*?每(?:个)?月.*?(\d+(?:\.\d+)?\s*(?:万|元|块|块钱)?)",
        r"每(?:个)?月.*?(?:花|支出|消费|用).*?(\d+(?:\.\d+)?\s*(?:万|元|块|块钱)?)",
        r"退休后.*?(?:花|支出|消费|用).*?(\d+(?:\.\d+)?\s*(?:万|元|块|块钱)?)"
    ]

    for pattern in goal_patterns:
        goal_match = re.search(pattern, q)
        if goal_match:
            amount = parse_amount_text(goal_match.group(1))
            if amount is not None and amount > 0:
                scenario["retirement_monthly_goal"] = amount
                break

    return scenario

# ------------------------------
# 问题类型识别：只判断用户要做什么任务
# ------------------------------
def parse_question_type(question):
    q = (question or "").strip().lower()
    user_id = extract_user_id(question or "")

    # 建议书优先级最高
    if any(k in q for k in ["建议书", "规划建议书", "养老规划报告", "规划报告"]):
        return "综合建议书生成"

    # 聚合查询（群体统计）
    agg_words = ["有多少客户", "客户有多少", "客户数量", "平均", "总和", "总计", "最大", "最小", "均值", "一共", "总共", "几个"]
    agg_scope = ["客户", "年龄", "净资产", "月收入", "月支出", "退休金", "企业年金", "风险评级", "浏览", "购买", "收藏", "次"]
    if user_id is None and (
            any(k in q for k in agg_words)
            or (("多少" in q or "几个" in q) and "客户" in q)
            or (("浏览" in q or "购买" in q or "收藏" in q or "看过" in q) and ("平均" in q or "多少" in q))
    ) and any(k in q for k in agg_scope):
        return "聚合查询"

    # 客户购买预测（未来）
    if any(k in q for k in [
        "未来一个星期", "未来一周", "未来 7 天", "未来7天", "未来七天",
        "下周", "接下来", "后续", "最可能购买", "最可能会买", "大概率会买",
        "预测", "会买什么", "可能买什么", "可能购买什么"
    ]):
        return "客户购买预测"

    # 客户行为偏好分析（历史）
    if any(k in q for k in [
        "行为最多", "最偏好", "更偏好", "偏好哪类", "最常浏览", "最常买", "买得最多",
        "购买最多", "更爱看", "感兴趣", "哪类产品行为最多", "对什么类型的产品行为最多"
    ]):
        return "客户行为偏好分析"

    # 投资配置建议
    if any(k in q for k in [
        "资产配置", "配置方案", "怎么配", "如何配置", "配什么产品",
        "全部投资", "只放", "定期存款", "能否达成目标", "达成目标", "不够换哪个产品",
        "如何调整", "怎么调整", "收益最大化", "投资收益最大化", "最小化风险", "风险波动",
        "增加什么产品", "增加什么配置", "加配什么产品"
    ]):
        return "投资配置建议"

    # 养老金缺口计算-最低积累
    if any(k in q for k in [
        "最低需要积攒", "最低需要攒", "最低积攒", "退休时最低", "至少得有多少养老钱",
        "最低要准备多少钱", "至少要准备多少钱", "养老缺口是多少", "要补多少钱", "补多少钱才够养老",
        "最低需要积攒多少钱", "至少要攒多少"
    ]):
        return "养老金缺口计算-最低积累"

    # 养老金缺口计算-可积累
    if any(k in q for k in [
        "可以积攒", "能积攒", "能攒下", "能攒多少", "退休时可以积攒", "退休时能攒",
        "到退休能积累多少钱", "到退休能攒多少", "能剩下多少养老本金"
    ]):
        return "养老金缺口计算-可积累"

    # 养老金缺口计算-月支出
    if (
            any(k in q for k in ["刚退休", "退休时", "退休后", "退休那天", "退休那个月"])
            and any(k in q for k in ["每月", "一个月", "月"])
            and any(k in q for k in ["支出", "花", "花销", "消费", "需要多少钱", "得花多少"])
    ):
        return "养老金缺口计算-月支出"

    # 客户信息查询-退休
    if any(k in q for k in [
        "距离退休还有多久", "退休还有多久", "还得上几年班", "多久退休", "什么时候退休", "还能工作几年"
    ]):
        return "客户信息查询-退休"

    # 客户信息查询-年龄
    if (
            any(k in q for k in ["年龄多大", "多大了", "几岁", "岁数", "现在年龄", "年龄"])
            and "平均" not in q
    ):
        return "客户信息查询-年龄"

    return "未知"


def fmt_money(value):
    if value is None:
        return "暂无数据"
    try:
        return f"{int(round(float(value))):,} 元"
    except Exception:
        return "暂无数据"


def fmt_percent(value):
    try:
        return f"{value * 100:.0f}%"
    except Exception:
        return "暂无数据"


def format_retirement_age(retirement_age_months):
    years = retirement_age_months // 12
    months = retirement_age_months % 12

    if months == 0:
        return f"{years} 岁"
    return f"{years} 岁 {months} 个月"


# ------------------------------
# 工具映射
# ------------------------------
def select_tools(qtype, scenario=None):
    tools_map = {
        "客户信息查询-年龄": ["BaseTableQuery"],
        "客户信息查询-退休": ["BaseTableQuery"],
        "客户行为偏好分析": ["ActionTableQuery"],
        "客户购买预测": ["ActionTableQuery", "BehaviorPredictor"],
        "养老金缺口计算-月支出": ["BaseTableQuery", "FinanceCalculator"],
        "养老金缺口计算-最低积累": ["BaseTableQuery", "FinanceCalculator"],
        "养老金缺口计算-可积累": ["BaseTableQuery", "FinanceCalculator"],
        "投资配置建议": ["BaseTableQuery", "FinanceCalculator", "AssetOptimizer"],
        "综合建议书生成": ["ReportGenerator", "BaseTableQuery", "FinanceCalculator", "ActionTableQuery",
                           "AssetOptimizer"]
    }

    tools = tools_map.get(qtype, [])

    if scenario:
        tools = ["ScenarioParser"] + tools

    return tools


# ------------------------------
# 延迟退休计算
# ------------------------------
def get_original_and_max_retirement_age(gender):
    if gender == "男":
        return 60, 63
    return 55, 58


def calculate_retirement_age_months(age, gender="男", scenario=None):
    if scenario is None:
        scenario = {}

    if "retirement_age" in scenario:
        return int(scenario["retirement_age"] * 12)

    age = int(round(age))
    original_age, max_age = get_original_and_max_retirement_age(gender)

    retire_year = 2025 + (original_age - age)
    retire_month = 3
    retire_day = 31

    retire_date = date(int(retire_year), retire_month, retire_day)
    policy_start = date(2025, 1, 1)

    months_diff = (retire_date.year - policy_start.year) * 12 + (retire_date.month - policy_start.month)
    months_diff = max(months_diff, 0)

    max_delay_months = (max_age - original_age) * 12
    delay_months = min(math.floor(months_diff / 4 + 0.5), max_delay_months)

    return original_age * 12 + delay_months


def calculate_time_to_retirement(age, gender="男", identity="干部", scenario=None):
    if scenario is None:
        scenario = {}

    age = int(round(age))
    retirement_age_months = calculate_retirement_age_months(age, gender, scenario)

    current_age_months = age * 12
    remaining_months_total = max(retirement_age_months - current_age_months, 0)

    remaining_years = remaining_months_total // 12
    remaining_months = remaining_months_total % 12
    return remaining_years, remaining_months


# ------------------------------
# 产品收益率中枢
# ------------------------------
def product_expected_return_median(product):
    r = product["expected_return"]

    if isinstance(r, (int, float)):
        return float(r)

    if isinstance(r, (list, tuple)) and len(r) == 2:
        return (float(r[0]) + float(r[1])) / 2

    raise ValueError(f"Cannot recognize expected_return: {r}")


def is_product_allowed(customer_risk, product):
    customer_level = RISK_ORDER.get(str(customer_risk), 0)
    product_min_level = min(RISK_ORDER.get(r, 99) for r in product["risk_level"])
    return product_min_level <= customer_level


# ------------------------------
# 通用金融计算
# ------------------------------
def get_customer_base_values(user_id, scenario=None):
    if scenario is None:
        scenario = {}

    age = query_customer_field(user_id, "Age")
    gender = query_customer_field(user_id, "Gender") or "男"
    monthly_expend = query_customer_field(user_id, "Monthly_Expend")
    pension = query_customer_field(user_id, "Pension")
    net_asset = query_customer_field(user_id, "Net_Asset")
    monthly_income = query_customer_field(user_id, "Monthly_Income")

    if age is None:
        return None

    age = int(round(age))
    retirement_age_months = calculate_retirement_age_months(age, gender, scenario)
    months_to_retirement = max(retirement_age_months - age * 12, 0)

    expected_age = scenario.get("expected_age", EXPECTED_AGE)
    retired_months = max(int(expected_age * 12 - retirement_age_months), 0)

    return {
        "age": age,
        "gender": gender,
        "monthly_expend": float(monthly_expend) if monthly_expend is not None else 0.0,
        "pension": float(pension) if pension is not None else 0.0,
        "net_asset": float(net_asset) if net_asset is not None else 0.0,
        "monthly_income": float(monthly_income) if monthly_income is not None else 0.0,
        "months_to_retirement": int(months_to_retirement),
        "retired_months": int(retired_months),
        "retirement_age_months": int(retirement_age_months),
        "expected_age": expected_age,
    }


def calculate_retired_monthly_expend(user_id, scenario=None):
    if scenario is None:
        scenario = {}

    vals = get_customer_base_values(user_id, scenario)
    if vals is None:
        return None

    if "retirement_monthly_goal" in scenario:
        return int(round(scenario["retirement_monthly_goal"]))

    months_to_retirement = vals["months_to_retirement"]

    if "inflation_rate" in scenario and "inflation_effective_after_years" in scenario:
        first_months = min(int(scenario["inflation_effective_after_years"] * 12), months_to_retirement)
        second_months = max(months_to_retirement - first_months, 0)

        retired_monthly = vals["monthly_expend"]
        retired_monthly *= (1 + INFLATION_RATE / 12) ** first_months
        retired_monthly *= (1 + scenario["inflation_rate"] / 12) ** second_months

    else:
        inflation_rate = scenario.get("inflation_rate", INFLATION_RATE)
        retired_monthly = vals["monthly_expend"] * ((1 + inflation_rate / 12) ** months_to_retirement)

    return int(round(retired_monthly))


def calculate_min_required_savings(user_id, scenario=None):
    if scenario is None:
        scenario = {}

    vals = get_customer_base_values(user_id, scenario)
    if vals is None:
        return None

    retired_monthly = calculate_retired_monthly_expend(user_id, scenario)
    if retired_monthly is None:
        return None

    retired_months = vals["retired_months"]

    # 通胀变化逻辑：
    # 1. 如果有 inflation_effective_after_years，说明 N 年后通胀变化；
    # 2. 如果只有 inflation_rate，说明从现在开始新通胀率立即生效；
    # 3. 退休后生活费也按新通胀率继续增长，并按默认投资回报率折现。
    if "inflation_rate" in scenario:
        new_inflation_rate = scenario["inflation_rate"]
        months_to_retirement = vals["months_to_retirement"]

        if "inflation_effective_after_years" in scenario:
            first_months = min(
                int(scenario["inflation_effective_after_years"] * 12),
                months_to_retirement
            )
            second_months = max(months_to_retirement - first_months, 0)
        else:
            first_months = 0
            second_months = months_to_retirement

        retired_monthly_float = vals["monthly_expend"]
        retired_monthly_float *= (1 + INFLATION_RATE / 12) ** first_months
        retired_monthly_float *= (1 + new_inflation_rate / 12) ** second_months

        life_need_pv = sum(
            retired_monthly_float
            * (((1 + new_inflation_rate / 12) / (1 + INVEST_RATE / 12)) ** k)
            for k in range(retired_months)
        )

        pension_pv = sum(
            vals["pension"] / ((1 + new_inflation_rate / 12) ** k)
            for k in range(retired_months)
        )

        return int(round(life_need_pv - pension_pv))

    # 常规 Q7 逻辑：没有通胀假设变化时，沿用题目示例口径
    total_need = retired_monthly * retired_months

    pv_pension = sum(
        vals["pension"] / ((1 + INFLATION_RATE / 12) ** k)
        for k in range(retired_months)
    )

    min_required = total_need - pv_pension
    return int(round(min_required))


def calculate_retirement_accumulation_by_rate(user_id, annual_rate, scenario=None):
    if scenario is None:
        scenario = {}

    vals = get_customer_base_values(user_id, scenario)
    if vals is None:
        return None

    monthly_surplus = max(vals["monthly_income"] - vals["monthly_expend"], 0)
    monthly_rate = annual_rate / 12

    fv_asset = vals["net_asset"] * ((1 + monthly_rate) ** vals["months_to_retirement"])

    if abs(monthly_rate) < 1e-12:
        fv_surplus = monthly_surplus * vals["months_to_retirement"]
    else:
        fv_surplus = monthly_surplus * (
                ((1 + monthly_rate) ** vals["months_to_retirement"] - 1) / monthly_rate
        )

    return int(round(fv_asset + fv_surplus))


# ------------------------------
# 建议书目标总结
# ------------------------------
def summarize_retirement_goal(question, user_id, scenario=None):
    if scenario is None:
        scenario = {}

    q = question.lower()

    retired_monthly = calculate_retired_monthly_expend(user_id, scenario)
    vals = get_customer_base_values(user_id, scenario)

    current_expend = vals["monthly_expend"] if vals else None

    goal_parts = []

    if "消费水平不下降" in q or "维持消费水平" in q:
        goal_parts.append("客户希望退休后消费水平不下降")

    if any(k in q for k in ["最小化风险波动", "风险波动", "稳健配置", "不希望资产波动"]):
        goal_parts.append("客户偏好稳健配置，关注资产波动控制")

    if any(k in q for k in ["追求投资收益最大化", "收益最大化", "收益最大"]):
        goal_parts.append("客户希望在风险承受范围内提高投资收益")

    if any(k in q for k in ["定期存款", "存款"]):
        goal_parts.append("客户关注低风险存款类产品是否足以覆盖养老目标")

    if any(k in q for k in ["流动性", "安全性"]):
        goal_parts.append("客户重视资金流动性和本金安全")

    if any(k in q for k in ["优先安全", "安全第一"]):
        goal_parts.append("客户希望养老资金优先保证安全，其次再考虑收益")

    if "retirement_monthly_goal" in scenario:
        goal_parts.append(f"客户希望退休后每月可支出 {fmt_money(scenario['retirement_monthly_goal'])}")

    if not goal_parts:
        goal_parts.append("退休后消费水平不下降")

    if current_expend is not None and retired_monthly is not None:
        return (
                "；".join(goal_parts)
                + f"。每月可花费与当前 {fmt_money(current_expend)}购买力相同的金额（退休时约为{fmt_money(retired_monthly)}）"
        )

    return "；".join(goal_parts) + "。"


def _normalize_other_advice_output(raw_text):
    text = (raw_text or "").strip()
    if not text:
        return ""

    if text.startswith("```"):
        text = re.sub(r"^```(?:text|markdown)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    advice_lines = [line for line in lines if line.startswith("•")]

    if len(advice_lines) < 3:
        return ""

    advice_lines = advice_lines[:5]
    return "7. 其他建议\n" + "\n".join(advice_lines)


def generate_other_advice_with_llm(report_context):
    user_prompt = (
        "以下是客户养老规划结构化信息，请仅基于这些事实生成第7章“其他建议”：\n"
        + json.dumps(report_context, ensure_ascii=False)
    )
    content = call_one_api_chat([
        {"role": "system", "content": OTHER_ADVICE_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ])
    normalized = _normalize_other_advice_output(content)
    if normalized:
        return normalized

    age = report_context.get("age")
    risk_level = report_context.get("risk_level", "暂无数据")
    years_to_retire = int(report_context.get("years_to_retire") or 0)
    gap = report_context.get("gap")
    behavior_preference = report_context.get("behavior_preference", "")
    allocation_plan = str(report_context.get("allocation_plan", "") or "")
    enterprise_ann = report_context.get("enterprise_ann", "无")

    suggestions = []

    if (isinstance(age, (int, float)) and age <= 35) or years_to_retire >= 20:
        suggestions.append("• 客户距退休时间较长，建议客户经理重点沟通尽早、持续进行养老储备，并通过长期纪律性投入提升资金积累效率。")

    if isinstance(gap, (int, float)) and gap > 0:
        suggestions.append("• 当前测算显示仍有养老资金缺口，建议客户经理引导客户提升每月养老储备并优化支出结构，同时按年度复盘目标达成进度。")
    elif isinstance(gap, (int, float)):
        suggestions.append("• 当前测算已可覆盖养老目标，建议客户经理与客户确认继续保持现有储蓄纪律，并按季度复盘资产配置与风险承受能力变化。")

    if risk_level in ("R1", "R2"):
        suggestions.append("• 客户风险评级偏稳健，建议后续以低风险、稳健型产品为主，并在沟通中充分说明产品风险收益特征与适配边界。")
    elif risk_level == "R3":
        suggestions.append("• 客户风险评级为R3，建议在稳健底仓基础上适度关注长期收益潜力产品，并保留必要流动性资产以应对阶段性资金需求。")
    elif risk_level in ("R4", "R5"):
        suggestions.append("• 客户风险承受能力较高，建议在评级允许范围内提升长期收益潜力配置，但仍保留养老资金的稳健底仓以降低过度波动影响。")

    if behavior_preference == "现金理财":
        suggestions.append("• 客户历史行为偏好现金理财，建议客户经理进一步确认其偏好来源（流动性、安全性或认知因素），再在合规前提下优化现金类资产占比。")

    if "年金险" in allocation_plan:
        suggestions.append("• 当前方案包含年金险，建议客户经理重点说明其在补充退休后长期现金流与应对长寿风险方面的作用，强化客户对配置目的的理解。")

    if enterprise_ann not in ("无", None, "", 0):
        suggestions.append("• 客户已具备企业年金安排，建议客户经理进一步确认领取时间、领取方式及税务规则，并纳入退休现金流统筹管理。")

    if len(suggestions) < 3:
        suggestions.append("• 建议客户经理与客户建立定期复盘机制，重点跟踪收入、支出、风险评级及养老目标变化，并在合规前提下动态优化既有配置。")

    return "7. 其他建议\n" + "\n".join(suggestions[:5])


# ------------------------------
# 养老金缺口计算 Skill
# ------------------------------
def pension_gap_calculation(user_id, question_type, scenario=None):
    if scenario is None:
        scenario = {}

    if question_type == "养老金缺口计算-月支出":
        retired_monthly = calculate_retired_monthly_expend(user_id, scenario)
        if retired_monthly is None:
            return "客户信息不足，无法计算"
        return f"{retired_monthly} 元"

    if question_type == "养老金缺口计算-最低积累":
        min_required = calculate_min_required_savings(user_id, scenario)
        if min_required is None:
            return "客户信息不足，无法计算"
        return f"{min_required} 元"

    if question_type == "养老金缺口计算-可积累":
        invest_rate = scenario.get("invest_rate", INVEST_RATE)
        total_savings = calculate_retirement_accumulation_by_rate(user_id, invest_rate, scenario)
        if total_savings is None:
            return "客户信息不足，无法计算"
        return f"{total_savings} 元"

    return "无法识别问题类型"


# ------------------------------
# 投资配置建议 Skill
# ------------------------------
def investment_allocation(user_id, question, scenario=None):
    if scenario is None:
        scenario = {}

    q = question.lower()

    # Q11：寿命延长，问最可能增加什么产品配置
    if scenario.get("expected_age", EXPECTED_AGE) > EXPECTED_AGE:
        if any(k in q for k in ["增加什么产品", "什么产品的配置", "最可能增加", "增加哪类产品"]):
            return "年金险"

    risk_level = query_customer_field(user_id, "Rsk_Cd")
    if not risk_level:
        return "未识别客户风险等级，无法提供资产配置建议"

    min_required = calculate_min_required_savings(user_id, scenario)
    if min_required is None:
        return "无法计算养老金最低积攒金额，暂不能提供资产配置建议"

    available_products = [
        p for p in PRODUCTS
        if is_product_allowed(risk_level, p)
    ]

    if not available_products:
        return "无符合客户风险等级的产品可配置"

    # Q9：全部投资定期存款能否达标，如不能如何调整
    if "定期存款" in q and any(k in q for k in ["能否",
                                                "能不能",
                                                "能不能够",
                                                "能达到",
                                                "达到",
                                                "达成",
                                                "达标",
                                                "够不够",
                                                "够吗",
                                                "够不",
                                                "够养老",
                                                "养老目标够",
                                                "能覆盖",]):
        deposit_product = next((p for p in PRODUCTS if p["name"] == "定期存款"), None)
        deposit_rate = product_expected_return_median(deposit_product)
        deposit_accum = calculate_retirement_accumulation_by_rate(user_id, deposit_rate, scenario)

        if deposit_accum is None:
            return "无法计算定期存款方案的退休积累金额"

        if deposit_accum >= min_required:
            return "能，全部投资定期存款可以达成养老目标"

        candidate_results = []
        for p in available_products:
            rate = product_expected_return_median(p)
            accum = calculate_retirement_accumulation_by_rate(user_id, rate, scenario)
            if accum is not None and accum >= min_required:
                candidate_results.append((p, rate, accum))

        if not candidate_results:
            return "不能，且客户当前风险等级范围内暂无可完全达成养老目标的产品"

        candidate_results.sort(key=lambda x: (RISK_ORDER.get(x[0]["risk_level"][0], 99), x[1]))
        best_product, best_rate, best_accum = candidate_results[0]

        return f"不能，需要改为投资{best_product['name']}"

    # Q12：追求投资收益最大化
    if any(k in q for k in ["最大化收益", "收益最大", "投资收益最大化"]):
        best_product = max(
            available_products,
            key=lambda p: product_expected_return_median(p)
        )
        return f"{best_product['name']}配置 100%"

    # Q13：满足养老需求基础上最小化风险波动
    if any(k in q for k in ["最小化风险", "风险波动"]):
        candidate_results = []

        for p in available_products:
            rate = product_expected_return_median(p)
            full_accum = calculate_retirement_accumulation_by_rate(user_id, rate, scenario)

            if full_accum is not None and full_accum >= min_required:
                candidate_results.append((p, rate, full_accum))

        if not candidate_results:
            return "客户当前风险等级范围内暂无可完全覆盖养老需求的产品，建议提高月结余或降低退休消费目标"

        candidate_results.sort(key=lambda x: x[1], reverse=True)
        main_product, main_rate, full_accum = candidate_results[0]

        main_pct = math.ceil(min_required / full_accum * 100)
        remain_pct = max(100 - main_pct, 0)

        allocation = {}
        allocation[main_product["name"]] = main_pct

        if remain_pct > 0:
            if main_product["name"] == "现金理财":
                allocation["年金险"] = allocation.get("年金险", 0) + remain_pct

            elif main_product["name"] == "年金险":
                allocation["现金理财"] = allocation.get("现金理财", 0) + remain_pct

            else:
                cash_pct = min(10, remain_pct)
                annuity_pct = remain_pct - cash_pct

                if cash_pct > 0:
                    allocation["现金理财"] = allocation.get("现金理财", 0) + cash_pct

                if annuity_pct > 0:
                    allocation["年金险"] = allocation.get("年金险", 0) + annuity_pct

        parts = []
        for product_name, pct in allocation.items():
            if product_name == main_product["name"]:
                parts.append(f"{product_name}配置 {pct}%")
            else:
                parts.append(f"{product_name} {pct}%")

        return "；".join(parts)

    # 默认：列出可投产品测算
    product_results = []
    for p in available_products:
        rate = product_expected_return_median(p)
        accum = calculate_retirement_accumulation_by_rate(user_id, rate, scenario)
        if accum is not None:
            product_results.append((p["name"], rate, accum))

    product_results.sort(key=lambda x: x[1], reverse=True)

    lines = []
    for name, rate, accum in product_results:
        status = "可达标" if accum >= min_required else "未达标"
        lines.append(f"{name}：收益率中枢{rate:.2%}，退休时预计积累{accum}元，{status}")

    return "；".join(lines)


# ------------------------------
# 单客户行为偏好分析 Skill
# ------------------------------
def customer_behavior_preference(user_id):
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cursor:
            sql = f"""
                SELECT
                    product,
                    COUNT(*) AS cnt,
                    SUM(CASE WHEN action_typ = '购买' THEN 1 ELSE 0 END) AS buy_cnt,
                    MAX(acs_tm) AS last_time,
                    MAX(priority) AS priority
                FROM (
                    SELECT
                        CASE
                            WHEN prod_sub_typ = '现金' THEN '现金理财'
                            WHEN prod_sub_typ = '一般性' AND prod_typ = '存款' THEN '定期存款'
                            WHEN prod_typ IN ('理财', '基金') AND rsk_lvl = 'R2' THEN '短债类产品'
                            WHEN prod_typ IN ('理财', '基金') AND rsk_lvl = 'R3' THEN '固收+产品'
                            WHEN prod_typ = '基金' AND rsk_lvl IN ('R4', 'R5') THEN '权益类产品'
                            WHEN prod_sub_typ IN ('税延养老年金', '养老年金') THEN '年金险'
                            ELSE '其他'
                        END AS product,
                        action_typ,
                        acs_tm,
                        CASE
                            WHEN prod_sub_typ IN ('税延养老年金', '养老年金') THEN 7
                            WHEN prod_typ IN ('理财', '基金') AND rsk_lvl = 'R3' THEN 6
                            WHEN prod_typ IN ('理财', '基金') AND rsk_lvl = 'R2' THEN 5
                            WHEN prod_typ = '基金' AND rsk_lvl IN ('R4', 'R5') THEN 4
                            WHEN prod_sub_typ = '一般性' AND prod_typ = '存款' THEN 3
                            WHEN prod_sub_typ = '现金' THEN 2
                            ELSE 1
                        END AS priority
                    FROM {ACTION_TABLE}
                    WHERE user_id = %s
                      AND prod_typ <> '非财富'
                ) T
                GROUP BY product
                ORDER BY cnt DESC, buy_cnt DESC, last_time DESC, priority DESC
                LIMIT 1
            """
            cursor.execute(sql, (user_id,))
            row = cursor.fetchone()

            if not row:
                return "未查询到该客户的财富产品行为记录"

            return row["product"]

    except Exception:
        return "行为偏好查询失败"

    finally:
        if conn:
            conn.close()



def customer_behavior_preference_detail(user_id):
    """
    返回客户行为最多的产品类别和行为次数。
    复用 customer_behavior_preference 的产品映射与排序逻辑，只是多返回 cnt。
    """
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cursor:
            sql = f"""
                SELECT
                    product,
                    COUNT(*) AS cnt,
                    SUM(CASE WHEN action_typ = '购买' THEN 1 ELSE 0 END) AS buy_cnt,
                    MAX(acs_tm) AS last_time,
                    MAX(priority) AS priority
                FROM (
                    SELECT
                        CASE
                            WHEN prod_sub_typ = '现金' THEN '现金理财'
                            WHEN prod_sub_typ = '一般性' AND prod_typ = '存款' THEN '定期存款'
                            WHEN prod_typ IN ('理财', '基金') AND rsk_lvl = 'R2' THEN '短债类产品'
                            WHEN prod_typ IN ('理财', '基金') AND rsk_lvl = 'R3' THEN '固收+产品'
                            WHEN prod_typ = '基金' AND rsk_lvl IN ('R4', 'R5') THEN '权益类产品'
                            WHEN prod_sub_typ IN ('税延养老年金', '养老年金') THEN '年金险'
                            ELSE '其他'
                        END AS product,
                        action_typ,
                        acs_tm,
                        CASE
                            WHEN prod_sub_typ IN ('税延养老年金', '养老年金') THEN 7
                            WHEN prod_typ IN ('理财', '基金') AND rsk_lvl = 'R3' THEN 6
                            WHEN prod_typ IN ('理财', '基金') AND rsk_lvl = 'R2' THEN 5
                            WHEN prod_typ = '基金' AND rsk_lvl IN ('R4', 'R5') THEN 4
                            WHEN prod_sub_typ = '一般性' AND prod_typ = '存款' THEN 3
                            WHEN prod_sub_typ = '现金' THEN 2
                            ELSE 1
                        END AS priority
                    FROM {ACTION_TABLE}
                    WHERE user_id = %s
                      AND prod_typ <> '非财富'
                ) T
                GROUP BY product
                ORDER BY cnt DESC, buy_cnt DESC, last_time DESC, priority DESC
                LIMIT 1
            """
            cursor.execute(sql, (user_id,))
            row = cursor.fetchone()

            if not row:
                return {
                    "product": "暂无明确偏好",
                    "count": 0
                }

            return {
                "product": row["product"],
                "count": int(row["cnt"])
            }

    except Exception:
        return {
            "product": "行为偏好查询失败",
            "count": 0
        }

    finally:
        if conn:
            conn.close()


def get_product_by_name(product_name):
    for product in PRODUCTS:
        if product["name"] == product_name:
            return product
    return None


def parse_allocation_plan(allocation_plan):
    """
    解析 investment_allocation 返回的配置文本。

    示例：
    固收+产品配置 73%；现金理财 10%；年金险 17%
    """
    result = []

    if not allocation_plan:
        return result

    parts = re.split(r"[；;]", allocation_plan)

    for part in parts:
        part = part.strip()
        if not part:
            continue

        match = re.search(r"(.+?)(?:配置)?\s*(\d+)%", part)
        if match:
            product_name = match.group(1).strip()
            pct = int(match.group(2))
            result.append({
                "product": product_name,
                "pct": pct
            })

    return result


def explain_allocation_with_gap_coverage(user_id, allocation_plan, scenario=None):
    """
    复用 investment_allocation 的配置结果，补充说明：
    1. 客户风险评级；
    2. 退休时最低需积攒金额；
    3. 各产品按配置比例预计形成退休时资产；
    4. 组合是否覆盖养老缺口。
    """
    if scenario is None:
        scenario = {}

    risk_level = query_customer_field(user_id, "Rsk_Cd") or "暂无数据"
    min_required = calculate_min_required_savings(user_id, scenario)

    parsed_plan = parse_allocation_plan(allocation_plan)

    if not parsed_plan:
        return (
            f"客户风险评级为 {risk_level}，以下方案已按客户风险承受能力筛选产品：\n"
            f"{allocation_plan}"
        )

    lines = []
    total_cover = 0

    for item in parsed_plan:
        product_name = item["product"]
        pct = item["pct"]

        product = get_product_by_name(product_name)

        if product is None:
            lines.append(f"• 将 {pct}% 配置于{product_name}。")
            continue

        rate = product_expected_return_median(product)
        full_accum = calculate_retirement_accumulation_by_rate(
            user_id,
            rate,
            scenario
        )

        if full_accum is None:
            cover_amount = None
        else:
            cover_amount = int(round(full_accum * pct / 100))
            total_cover += cover_amount

        lines.append(
            f"• 将 {pct}% 配置于{product_name}（收益率中枢 {rate:.2%}），"
            f"按该比例预计可形成退休时资产约 {fmt_money(cover_amount)}。"
        )

    if min_required is None:
        coverage_summary = "暂无法测算该配置组合对养老缺口的覆盖情况。"
    elif total_cover >= min_required:
        coverage_summary = (
            f"按上述配置测算，组合预计形成退休时资产约 {fmt_money(total_cover)}。"
        )
    else:
        gap = min_required - total_cover
        coverage_summary = (
            f"按上述配置测算，组合预计形成退休时资产约 {fmt_money(total_cover)}，"
            f"距离退休时最低需积攒金额 {fmt_money(min_required)} 仍差约 {fmt_money(gap)}。"
        )

    return (
        f"客户风险评级为 {risk_level}，以下方案已按客户风险承受能力筛选产品：\n"
        + "\n".join(lines)
        + "\n"
        + coverage_summary
    )


# ------------------------------
# 客户购买预测 Skill
# ------------------------------
def customer_purchase_prediction(user_id):
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cursor:
            sql = f"""
                SELECT
                    product,
                    SUM(score) AS total_score,
                    SUM(CASE WHEN action_typ = '购买' THEN 1 ELSE 0 END) AS buy_cnt,
                    COUNT(*) AS cnt,
                    MAX(acs_tm) AS last_time,
                    MAX(priority) AS priority
                FROM (
                    SELECT
                        CASE
                            WHEN prod_sub_typ = '现金' THEN '现金理财'
                            WHEN prod_sub_typ = '一般性' AND prod_typ = '存款' THEN '定期存款'
                            WHEN prod_typ IN ('理财', '基金') AND rsk_lvl = 'R2' THEN '短债类产品'
                            WHEN prod_typ IN ('理财', '基金') AND rsk_lvl = 'R3' THEN '固收+产品'
                            WHEN prod_typ = '基金' AND rsk_lvl IN ('R4', 'R5') THEN '权益类产品'
                            WHEN prod_sub_typ IN ('税延养老年金', '养老年金') THEN '年金险'
                            ELSE '其他'
                        END AS product,
                        action_typ,
                        acs_tm,
                        CASE
                            WHEN action_typ = '购买' THEN 5
                            WHEN action_typ = '收藏' THEN 3
                            WHEN action_typ IN ('浏览详情', '浏览持仓') THEN 1
                            ELSE 1
                        END AS score,
                        CASE
                            WHEN prod_sub_typ IN ('税延养老年金', '养老年金') THEN 7
                            WHEN prod_typ IN ('理财', '基金') AND rsk_lvl = 'R3' THEN 6
                            WHEN prod_typ IN ('理财', '基金') AND rsk_lvl = 'R2' THEN 5
                            WHEN prod_typ = '基金' AND rsk_lvl IN ('R4', 'R5') THEN 4
                            WHEN prod_sub_typ = '一般性' AND prod_typ = '存款' THEN 3
                            WHEN prod_sub_typ = '现金' THEN 2
                            ELSE 1
                        END AS priority
                    FROM {ACTION_TABLE}
                    WHERE user_id = %s
                      AND prod_typ <> '非财富'
                ) T
                GROUP BY product
                ORDER BY buy_cnt DESC, total_score DESC, last_time DESC, priority DESC
                LIMIT 1
            """
            cursor.execute(sql, (user_id,))
            row = cursor.fetchone()

            if not row:
                return "未查询到该客户的财富产品行为记录"

            return row["product"]

    except Exception:
        return "购买预测失败"

    finally:
        if conn:
            conn.close()


# ------------------------------
# 建议书生成 Skill
# ------------------------------

def generate_retirement_report(user_id, question, scenario=None):
    if scenario is None:
        scenario = {}

    vals = get_customer_base_values(user_id, scenario)
    if vals is None:
        return "客户基础信息不足，无法生成养老规划建议书"

    age = vals["age"]
    gender = vals["gender"]
    risk_level = query_customer_field(user_id, "Rsk_Cd") or "暂无数据"
    net_asset = vals["net_asset"]
    monthly_income = vals["monthly_income"]
    monthly_expend = vals["monthly_expend"]
    monthly_surplus = monthly_income - monthly_expend
    pension = vals["pension"]
    enterprise_ann = query_customer_field(user_id, "Enterprise_Ann")

    if enterprise_ann is None or float(enterprise_ann) == 0:
        enterprise_ann_text = "无"
    else:
        enterprise_ann_text = fmt_money(enterprise_ann)

    retirement_age_months = vals["retirement_age_months"]
    retirement_age_text = format_retirement_age(retirement_age_months)

    years_to_retire, months_to_retire = calculate_time_to_retirement(age, gender, scenario=scenario)
    if months_to_retire == 0:
        retire_distance_text = f"{years_to_retire} 年"
    else:
        retire_distance_text = f"{years_to_retire} 年 {months_to_retire} 月"

    retired_monthly = calculate_retired_monthly_expend(user_id, scenario)
    min_required = calculate_min_required_savings(user_id, scenario)
    total_savings = calculate_retirement_accumulation_by_rate(
        user_id,
        scenario.get("invest_rate", INVEST_RATE),
        scenario
    )

    retired_months = vals["retired_months"]
    total_need = retired_monthly * retired_months if retired_monthly is not None else None

    inflation_rate = scenario.get("inflation_rate", INFLATION_RATE)
    pension_pv = sum(
        pension / ((1 + inflation_rate / 12) ** k)
        for k in range(retired_months)
    )

    if min_required is not None and total_savings is not None:
        gap = min_required - total_savings
    else:
        gap = None

    if gap is None:
        gap_text = "暂无法判断资金缺口。"
    elif gap > 0:
        gap_text = f"仍存在约 {fmt_money(gap)} 资金缺口。"
    else:
        gap_text = "预计可以覆盖养老目标。"

    behavior_detail = customer_behavior_preference_detail(user_id)
    behavior_preference = behavior_detail["product"]
    behavior_count = behavior_detail["count"]

    # 资产配置方案：如果当前问题没明确偏好，默认用最小化风险波动方案
    q = question.lower()
    if any(k in q for k in ["最小化风险", "风险波动", "稳健", "不希望资产波动"]):
        allocation_question = f"客户 {user_id} 想要在满足养老需求基础上最小化风险波动，请为他提供资产配置方案。"
        allocation_title = "客户偏好最小化风险方案"
    elif any(k in q for k in ["最大化收益", "收益最大", "追求投资收益"]):
        allocation_question = f"客户 {user_id} 想要追求投资收益最大化，请为他提供资产配置方案。"
        allocation_title = "客户偏好收益最大化方案"
    elif "定期存款" in q:
        allocation_question = f"客户 {user_id} 想要退休后维持消费水平不下降，如果全部投资定期存款，他能否达成目标？如不能，他要如何调整？"
        allocation_title = "客户关注定期存款方案"
    else:
        allocation_question = f"客户 {user_id} 想要在满足养老需求基础上最小化风险波动，请为他提供资产配置方案。"
        allocation_title = "客户想要满足养老目标基础上的稳健配置方案"

    allocation_plan = investment_allocation(user_id, allocation_question, scenario)
    allocation_plan_detail = explain_allocation_with_gap_coverage(user_id, allocation_plan, scenario)
    report_context = {
        "user_id": user_id,
        "age": age,
        "risk_level": risk_level,
        "retire_distance": retire_distance_text,
        "years_to_retire": years_to_retire,
        "monthly_surplus": monthly_surplus,
        "min_required": min_required,
        "total_savings": total_savings,
        "gap": gap,
        "behavior_preference": behavior_preference,
        "behavior_count": behavior_count,
        "allocation_plan": allocation_plan_detail,
        "goal_summary": summarize_retirement_goal(question, user_id, scenario),
        "enterprise_ann": enterprise_ann_text,
    }
    other_advice_text = generate_other_advice_with_llm(report_context)

    report = f"""1. 基本情况
客户 ID：{user_id}，年龄：{age} 岁，性别：{gender}，风险评级：{risk_level}。当前净资产：{fmt_money(net_asset)}，每月结余：{fmt_money(monthly_surplus)}（月收入 {fmt_money(monthly_income)} − 月支出 {fmt_money(monthly_expend)}）。每月退休金：{fmt_money(pension)}，企业年金（一次性提取）：{enterprise_ann_text}。

2. 基本假设
预期寿命 {scenario.get("expected_age", EXPECTED_AGE)} 岁，长期通胀率 {fmt_percent(INFLATION_RATE)}，退休年龄为 {retirement_age_text}岁，距退休约 {retire_distance_text}。

3. 养老目标
{summarize_retirement_goal(question, user_id, scenario)}

4. 退休后财富需求测算
退休后预计总需求约 {fmt_money(total_need)}。其中退休金（先付年金现值）可支撑约 {fmt_money(pension_pv)}，还有 {fmt_money(min_required)}缺口需要通过投资积累来覆盖。
5. 产品偏好
根据客户历史浏览、购买、收藏等行为记录，客户对{behavior_preference}类产品行为最多，共 {behavior_count} 次相关行为，推测其更偏好{behavior_preference}类产品。

6. 资产配置方式与具体方案
{allocation_title}：
{allocation_plan_detail}

{other_advice_text}"""

    return report


# ------------------------------
# 主处理函数
# ------------------------------
def handle_question(question):
    user_id = extract_user_id(question)
    scenario = parse_scenario_overrides(question)

    # 规则分类 + LLM fallback 分类
    qtype = get_question_type(question)

    # 1. 聚合查询
    if qtype == "聚合查询":
        aggregation_raw = generate_aggregation_sql_json(question)
        try:
            aggregation_meta = json.loads(aggregation_raw)
        except json.JSONDecodeError as e:
            print(f"WARNING: 聚合查询 JSON 解析失败: {e}", file=sys.stderr)
            aggregation_meta = dict(AGGREGATION_FALLBACK)

        if not aggregation_meta.get("is_aggregation_query") or not aggregation_meta.get("sql"):
            print("暂不支持该聚合查询")
            return

        result = execute_aggregation_query(str(aggregation_meta.get("sql", "")))
        print(format_aggregation_answer(aggregation_meta, result))
        return

    answer = "暂不支持该问题类型，或缺少客户ID"

    # 2. 综合建议书
    if user_id and qtype == "综合建议书生成":
        answer = generate_retirement_report(user_id, question, scenario)

    # 3. 养老金缺口计算
    elif user_id and qtype in [
        "养老金缺口计算-月支出",
        "养老金缺口计算-最低积累",
        "养老金缺口计算-可积累",
    ]:
        answer = pension_gap_calculation(user_id, qtype, scenario)

    # 4. 客户信息查询-年龄
    elif user_id and qtype == "客户信息查询-年龄":
        age_val = query_customer_field(user_id, "Age")
        if age_val is not None:
            answer = f"{int(round(age_val))} 岁"
        else:
            answer = "年龄未知，无法计算"

    # 5. 客户信息查询-退休
    elif user_id and qtype == "客户信息查询-退休":
        age_val = query_customer_field(user_id, "Age")
        gender = query_customer_field(user_id, "Gender") or "男"

        if age_val is not None:
            years, months = calculate_time_to_retirement(age_val, gender, scenario=scenario)
            answer = f" {years} 年 {months} 月"
        else:
            answer = "年龄未知，无法计算"

    # 6. 投资配置建议
    elif user_id and qtype == "投资配置建议":
        answer = investment_allocation(user_id, question, scenario)

    # 7. 客户行为偏好分析
    elif user_id and qtype == "客户行为偏好分析":
        answer = customer_behavior_preference(user_id)

    # 8. 客户购买预测
    elif user_id and qtype == "客户购买预测":
        answer = customer_purchase_prediction(user_id)

    # 9. 已识别类型但缺少客户ID
    elif qtype != "未知" and not user_id:
        answer = "未识别客户ID，无法回答该问题"

    else:
        answer = "抱歉，我无法理解该问题"

    print(f"问题类型：{qtype}")
    print(answer)
# ------------------------------
# 命令行运行
# ------------------------------
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('请在命令行传入问题，例如：python3 run.py "客户V500001现在年龄多大？"')
        sys.exit(1)

    handle_question(sys.argv[1])
