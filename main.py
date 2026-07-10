"""
DeskSage 工位风水诊断 - 商业版 v2
支持八字 + 工位合断
Vercel 兼容版本 - 使用内存存储替代文件存储
"""
import os
import uuid
import json
import asyncio
import secrets
import string
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, UploadFile, File, HTTPException, Header, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import httpx
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
ADMIN_TOKEN = "admin-secret-token-2024"

# ============ 密钥存储（从环境变量加载，Vercel 多实例共享）============
DEFAULT_KEYS = [
    {"key": "TEST-AAAA-BBBB-CCCC", "created_at": "2026-01-01T00:00:00", "expires_at": "2028-12-31T23:59:59", "used_count": 0, "note": "测试密钥", "active": True},
    {"key": "TEST-DDDD-EEEE-FFFF", "created_at": "2026-01-01T00:00:00", "expires_at": "2028-12-31T23:59:59", "used_count": 0, "note": "测试密钥", "active": True},
]

def _load_keys_from_env() -> list:
    """从环境变量 KEYS_JSON 加载密钥列表"""
    env_keys = os.environ.get("KEYS_JSON", "")
    if env_keys:
        try:
            return json.loads(env_keys)
        except (json.JSONDecodeError, TypeError):
            pass
    return DEFAULT_KEYS

# 内存中的动态密钥（后台创建的）
IN_MEMORY_KEYS = {"keys": list(_load_keys_from_env())}

def _get_all_keys() -> list:
    """合并环境变量密钥和内存密钥，去重"""
    env_keys = _load_keys_from_env()
    mem_keys = IN_MEMORY_KEYS.get("keys", [])
    seen = set()
    merged = []
    for k in env_keys + mem_keys:
        if k["key"] not in seen:
            seen.add(k["key"])
            merged.append(k)
    return merged
IN_MEMORY_REPORTS: dict = {}

VERCEL_API_TOKEN = os.environ.get("VERCEL_API_TOKEN", "")
VERCEL_PROJECT_ID = os.environ.get("VERCEL_PROJECT_ID", "")

async def _update_vercel_env(keys_json: str):
    """更新 Vercel 环境变量 KEYS_JSON"""
    if not VERCEL_API_TOKEN or not VERCEL_PROJECT_ID:
        print("WARNING: VERCEL_API_TOKEN or VERCEL_PROJECT_ID not set")
        return False
    try:
        import httpx
        headers = {"Authorization": f"Bearer {VERCEL_API_TOKEN}"}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"https://api.vercel.com/v9/projects/{VERCEL_PROJECT_ID}/env",
                headers=headers
            )
            envs = r.json().get("envs", [])
            key_env = None
            for env in envs:
                if env.get("key") == "KEYS_JSON":
                    key_env = env
                    break

            if key_env:
                await client.patch(
                    f"https://api.vercel.com/v9/projects/{VERCEL_PROJECT_ID}/env/{key_env['id']}",
                    headers=headers,
                    json={"value": keys_json, "target": ["production"]}
                )
            else:
                await client.post(
                    f"https://api.vercel.com/v9/projects/{VERCEL_PROJECT_ID}/env",
                    headers=headers,
                    json={
                        "key": "KEYS_JSON",
                        "value": keys_json,
                        "type": "plain",
                        "target": ["production"]
                    }
                )
            # 通过 GitHub API 推送空 commit 触发 Vercel 重新部署
            try:
                github_token = os.environ.get("GITHUB_TOKEN", "")
                if github_token:
                    gh_headers = {"Authorization": f"token {github_token}", "Accept": "application/vnd.github.v3+json"}
                    # 获取 main 分支最新 SHA
                    ref_resp = await client.get(
                        "https://api.github.com/repos/wuy20041029-hub/desksage-api/git/ref/heads/main",
                        headers=gh_headers
                    )
                    sha = ref_resp.json()["object"]["sha"]
                    # 创建空 commit
                    commit_resp = await client.post(
                        "https://api.github.com/repos/wuy20041029-hub/desksage-api/git/commits",
                        headers=gh_headers,
                        json={"message": "sync: update keys from admin", "tree": sha, "parents": [sha]}
                    )
                    commit_sha = commit_resp.json().get("sha", "")
                    if commit_sha:
                        # 更新 ref
                        await client.patch(
                            "https://api.github.com/repos/wuy20041029-hub/desksage-api/git/ref/heads/main",
                            headers=gh_headers,
                            json={"sha": commit_sha}
                        )
            except Exception as e:
                print(f"GitHub deploy trigger error: {e}")
            return True
    except Exception as e:
        print(f"Vercel API error: {e}")
        return False


app = FastAPI(title="DeskSage API", version="2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


class KeyCreate(BaseModel):
    days: int = 30
    count: int = 1
    note: str = ""


# ============ 密钥管理 ============
def load_keys() -> dict:
    # 返回合并后的密钥列表
    all_keys = _get_all_keys()
    IN_MEMORY_KEYS["keys"] = all_keys
    return IN_MEMORY_KEYS

def save_keys(data: dict):
    IN_MEMORY_KEYS["keys"] = data["keys"]

async def save_keys_remote(data: dict):
    """保存密钥并同步到 Vercel 环境变量"""
    IN_MEMORY_KEYS["keys"] = data["keys"]
    keys_json = json.dumps(data["keys"], ensure_ascii=False)
    await _update_vercel_env(keys_json)

def generate_key() -> str:
    chars = string.ascii_uppercase + string.digits
    return "-".join(["".join(secrets.choice(chars) for _ in range(4)) for _ in range(4)])

def verify_key(key: str) -> dict:
    data = load_keys()
    for k in data["keys"]:
        if k["key"] == key:
            if not k["active"]: return {"valid": False, "reason": "密钥已停用"}
            if datetime.now() > datetime.fromisoformat(k["expires_at"]):
                return {"valid": False, "reason": "密钥已过期"}
            return {"valid": True, "info": k}
    return {"valid": False, "reason": "密钥不存在"}

async def use_key(key: str):
    data = load_keys()
    for k in data["keys"]:
        if k["key"] == key:
            k["used_count"] += 1
            break
    save_keys(data)


# ============ 八字计算 ============
HEAVENLY_STEMS = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸"]
EARTHLY_BRANCHES = ["子", "丑", "寅", "卯", "辰", "巳", "午", "未", "申", "酉", "戌", "亥"]
WUXING_MAP = {
    "甲": "mu", "乙": "mu", "寅": "mu", "卯": "mu",
    "丙": "huo", "丁": "huo", "巳": "huo", "午": "huo",
    "戊": "tu", "己": "tu", "辰": "tu", "戌": "tu", "丑": "tu", "未": "tu",
    "庚": "jin", "辛": "jin", "申": "jin", "酉": "jin",
    "壬": "shui", "癸": "shui", "亥": "shui", "子": "shui"
}
WUXING_LABEL = {"jin": "金", "mu": "木", "shui": "水", "huo": "火", "tu": "土"}

SHICHEN_LABEL = {
    "23-01": "子时", "01-03": "丑时", "03-05": "寅时", "05-07": "卯时",
    "07-09": "辰时", "09-11": "巳时", "11-13": "午时", "13-15": "未时",
    "15-17": "申时", "17-19": "酉时", "19-21": "戌时", "21-23": "亥时"
}


def calculate_bazi(birthdate: str, birthtime: str, gender: str, name: str = "") -> dict:
    """简化的八字计算（生产环境接入真实万年历）"""
    # 模拟：根据日期生成合理的八字
    try:
        dt = datetime.fromisoformat(birthdate)
        year = dt.year
        month = dt.month
        day = dt.day

        # 年柱
        year_gan_idx = (year - 4) % 10
        year_zhi_idx = (year - 4) % 12
        year_pillar = HEAVENLY_STEMS[year_gan_idx] + EARTHLY_BRANCHES[year_zhi_idx]

        # 月柱（简化）
        month_gan_idx = (year_gan_idx * 2 + month) % 10
        month_zhi_idx = (month + 1) % 12
        month_pillar = HEAVENLY_STEMS[month_gan_idx] + EARTHLY_BRANCHES[month_zhi_idx]

        # 日柱（用日期做种子）
        day_gan_idx = (year + month * 31 + day) % 10
        day_zhi_idx = (year + month * 31 + day) % 12
        day_pillar = HEAVENLY_STEMS[day_gan_idx] + EARTHLY_BRANCHES[day_zhi_idx]

        # 时柱
        shichen_idx = {"23-01": 0, "01-03": 1, "03-05": 2, "05-07": 3,
                       "07-09": 4, "09-11": 5, "11-13": 6, "13-15": 7,
                       "15-17": 8, "17-19": 9, "19-21": 10, "21-23": 11}.get(birthtime, 0)
        time_gan_idx = (day_gan_idx * 2 + shichen_idx) % 10
        time_pillar = HEAVENLY_STEMS[time_gan_idx] + EARTHLY_BRANCHES[shichen_idx]

        bazi = [year_pillar, month_pillar, day_pillar, time_pillar]
        day_master = day_pillar[0]

        # 五行统计
        wuxing_count = {"jin": 0, "mu": 0, "shui": 0, "huo": 0, "tu": 0}
        for pillar in bazi:
            for ch in pillar:
                if ch in WUXING_MAP:
                    wuxing_count[WUXING_MAP[ch]] += 1

        # 喜用神（简化：缺什么补什么）
        weakest = min(wuxing_count, key=lambda k: wuxing_count[k])
        favorable = WUXING_LABEL[weakest]

        # 合断结论
        synthesis = generate_synthesis(day_master, wuxing_count, favorable)

        return {
            "name": name or "命主",
            "birthdate": birthdate,
            "birthtime": birthtime,
            "birthtime_label": SHICHEN_LABEL.get(birthtime, ""),
            "gender": gender,
            "bazi": bazi,
            "day_master": day_master,
            "wuxing": wuxing_count,
            "favorable": favorable,
            "synthesis": synthesis
        }
    except Exception as e:
        return {
            "name": name or "命主",
            "birthdate": birthdate,
            "birthtime": birthtime,
            "birthtime_label": SHICHEN_LABEL.get(birthtime, ""),
            "gender": gender,
            "bazi": ["甲子", "丙寅", "戊辰", "壬戌"],
            "day_master": "戊",
            "wuxing": {"jin": 1, "mu": 2, "shui": 1, "huo": 1, "tu": 3},
            "favorable": "金",
            "synthesis": "命主日主戊土，生于春季，木气旺盛而土受克。喜金以制木，土为比助亦佳。工位宜方正稳重，色调以黄白为上，忌东向背光。"
        }


def generate_synthesis(day_master: str, wuxing: dict, favorable: str) -> str:
    """生成八字与工位的合断文本"""
    wuxing_desc = "、".join([f"{WUXING_LABEL[k]}{v}个" for k, v in wuxing.items() if v > 0])

    strongest = max(wuxing, key=lambda k: wuxing[k])
    weakest = min(wuxing, key=lambda k: wuxing[k])

    return (
        f"命主日主 {day_master}，命局五行分布为 {wuxing_desc}。"
        f"其中{WUXING_LABEL[strongest]}气最旺，{WUXING_LABEL[weakest]}气最弱。"
        f"喜用神为【{favorable}】，故工位调候当以{WUXING_LABEL[strongest]}气平衡、{favorable}气补益为主旨。"
        f"桌面陈设宜简素方正，色调可取{WUXING_LABEL[strongest == 'mu' and 'jin' or strongest]}色系以扶助命主。"
    )


# ============ Agent 调用（模拟）============
async def call_dev_engineer() -> dict:
    await asyncio.sleep(2.5)
    return {
        "photo_quality": "清晰",
        "seat_analysis": {
            "facing": "面朝窗户",
            "back_to": "背对实墙",
            "has_support": True,
            "has_rush": False,
            "note": "座位布局端正，背有实墙为靠山",
            "harm": "若背向门坐，则来者不知，气场易被惊扰，且背后无靠，安全感不足，决策时易生疑虑。",
            "benefit": "背有实墙如背后有靠山，工作时心神安定，得贵人扶持之力，处事果断。"
        },
        "desktop_analysis": {
            "items": ["笔记本电脑", "文件堆（右侧）", "塑料假花", "空水杯", "手机充电器"],
            "left_right": "右侧明显高于左侧",
            "clutter_score": 7,
            "note": "右侧文件堆积过高，青龙位受压",
            "harm": "右侧过高为'白虎压青龙'之象，主口舌纷争、决策受压。青龙主生机文昌，受压则思路受阻、贵人远离。",
            "benefit": "若改作左侧略高、右侧略低，呈'龙高虎伏'之势，则主文昌利考、人际和顺、决策得助。"
        },
        "plant_decoration": {
            "has_plant": True,
            "plant_type": "塑料假花",
            "plant_status": "不适用",
            "sharp_objects": ["无"],
            "water_feature": False,
            "note": "假花缺乏生气",
            "harm": "塑料假花五行属'火'，且无生机可生，长期相伴则气场渐燥，心绪浮而不宁，徒有其形而无其神。",
            "benefit": "真绿植可活化气场、净化空气、调养眼目，更可催旺东方青龙木气，使人神清气爽、思虑清明。"
        },
        "overhead": {
            "beam": False,
            "light_type": "长条形LED灯",
            "light_direct": False,
            "air_con": True,
            "air_con_direct": True,
            "note": "空调直吹头部",
            "harm": "空调冷风直吹头部为'冷风压顶'，主头痛颈僵、睡眠不安，长期则思考力减弱、贵人远离。",
            "benefit": "若空调出风口调至侧方或加导风板，则头顶气场平和，脑力清明，与上司沟通顺畅。"
        },
        "lighting": {
            "source": "混合光",
            "brightness": "适中",
            "backlight": False,
            "note": "光线条件良好",
            "harm": "若光线过暗则阴气凝聚，人易倦怠懒散；过亮则心神外散，难专注。",
            "benefit": "明暗适度、自然光为上，气场清朗则思路敏捷、贵人明鉴。"
        },
        "background": {
            "color": "白色",
            "decorations": ["无"],
            "special": ["无"],
            "note": "墙面空白",
            "harm": "墙为'靠山'，全白无饰则靠山无力，且独坐时背影空虚，难获后援。",
            "benefit": "依命主喜用神择画挂之，背后有情，则坐而无忧，后援绵长。"
        },
        "overall_score": 55,
        "risk_factors": ["右侧过高", "假花失气", "空调直吹", "墙面空缺"],
        "advantages": ["背有实墙", "光线适中", "视野开阔"]
    }


async def call_visual_designer(scan: dict, bazi: dict) -> dict:
    await asyncio.sleep(2.0)
    favorable = bazi.get("favorable", "金")
    fav_lower = favorable.lower() if favorable.lower() in WUXING_LABEL else "mu"
    fav_label = WUXING_LABEL[fav_lower]

    # 根据喜用神推荐具体盆栽和摆件
    plant_map = {
        "mu": {"name": "小绿萝/文竹", "desc": "叶片舒展，青龙木气最旺", "price": "15-25元"},
        "huo": {"name": "红掌/朱蕉", "desc": "色应南方火气，催旺事业热情", "price": "20-35元"},
        "tu": {"name": "虎皮兰/仙人球（小型）", "desc": "敦厚稳重，培土固基", "price": "15-25元"},
        "jin": {"name": "白色蝴蝶兰/银皇后", "desc": "金气清朗，利决断", "price": "25-40元"},
        "shui": {"name": "富贵竹（6支）", "desc": "水气流通，财源广进", "price": "20-30元"}
    }
    ornament_map = {
        "mu": {"name": "桃木如意小摆件", "desc": "助木气生发，利人际", "price": "30-50元"},
        "huo": {"name": "红玛瑙文昌塔", "desc": "催旺事业热情与决策力", "price": "40-80元"},
        "tu": {"name": "黄玉貔貅小摆件", "desc": "稳固气场，聚财守成", "price": "50-100元"},
        "jin": {"name": "金属文昌塔/铜葫芦", "desc": "金气清肃，利文书与决策", "price": "30-60元"},
        "shui": {"name": "黑曜石小鱼缸", "desc": "水气流通，财源活水", "price": "40-80元"}
    }

    plant = plant_map.get(fav_lower, plant_map["mu"])
    ornament = ornament_map.get(fav_lower, ornament_map["mu"])

    return {
        "overview": {
            "before": "右侧文卷堆积成山、塑料假花枯寂、空调冷风直贯头顶、墙面空寂无依",
            "after": f"扶助{fav_label}气生发，左右平衡有度，明堂开阔纳气",
            "score": 8.5,
            "harm_summary": "当下工位主格局为'龙陷虎威'，主事业发展受压、思路受阻、人际疏离；若不改之，三月内易见工作调度不顺、上司责难之象。",
            "benefit_summary": "调候之后，可成'龙腾虎伏'之局，主思路敏捷、贵人明鉴、事业渐入佳境，预计半年内可见事业转折之机。"
        },
        "immediate_actions": [
            "将右侧文卷移至左侧青龙位之下，使左高右低、龙高虎伏",
            "撤去塑料假花，清理桌面，留出'明堂'区域（显示器正前方）",
            "调整空调出风方向，避开头顶，使其斜吹墙面"
        ],
        "this_week": [
            {
                "name": plant["name"],
                "location": "桌面左前方青龙位",
                "purpose": plant["desc"],
                "price": plant["price"],
                "spec": "小型盆栽（高度15-25cm），带盆托"
            },
            {
                "name": "桌面收纳盒（实木或藤编三件套）",
                "location": "替换零散文件",
                "purpose": "整理纳气，使明堂开阔有度",
                "price": "30-50元",
                "spec": "建议原木色或浅黄色"
            },
            {
                "name": ornament["name"],
                "location": "桌面右后方白虎位",
                "purpose": ornament["desc"],
                "price": ornament["price"],
                "spec": "高度8-12cm小巧精致款"
            }
        ],
        "this_month": [
            {"name": f"喜用神{fav_label}色系装饰画", "location": "背后墙面（坐向正对）", "purpose": "补益靠山，催旺命局", "price": "40-80元", "spec": "建议尺寸40×60cm，简约山水或抽象画"},
            {"name": "桌面香薰（小容量）", "location": "桌面右侧", "purpose": "调和气场，安神定志", "price": "30-50元", "spec": "木质调或檀香为上，避开浓香型"}
        ],
        "long_term": [
            "每周五下班前清理桌面，使明堂开阔有度",
            "绿植每周浇水，枯叶即剪，半年更换一次",
            "每月初一检视布局，依当月气场微调",
            "每季度依流年流月调整主摆件位置"
        ],
        "ai_prompt": "A modern minimalist Chinese style office desk with green pothos plant on left, natural wood storage boxes, jade ornament on right, soft natural window light, feng shui optimized layout with balanced yin-yang energy, photorealistic, 8k, warm earth tones --ar 16:9",
        "layout": """
         [窗户 - 自然光源 ↑]
    ┌──────────────────────────┐
    │ [绿植]  [显示器]  [摆件] │
    │          [键盘]           │
    │  [水杯]   [鼠标] [香薰]   │
    │         ← 明堂 →         │
    └──────────────────────────┘
            [背有实墙 ✓]
    """
    }


async def call_project_director(scan: dict, design: dict, bazi: dict) -> dict:
    await asyncio.sleep(1.5)
    return {
        "overall": {"status": "通过", "score": 86},
        "final": {
            "summary": f"工位综合评分55分，主因右侧过高、假花失气、空调直吹。结合命主八字（喜用神{bazi.get('favorable', '金')}），改造后预期可提升至85分以上。",
            "top3": [
                f"立即做：清右侧文卷、撤假花、调空调风向",
                f"本周做：置真绿植于青龙位，补{bazi.get('favorable', '金')}气摆件",
                f"长期养：每周整理，依流年微调"
            ],
            "closing": "格局可改，气运可调。命由天定，运由己造。工位虽小，乃事业之映射，望君珍重调候之机。"
        }
    }


# ============ 任务状态 ============
tasks: dict = {}

def init_task(task_id: str):
    tasks[task_id] = {
        "status": "pending", "progress": 0, "current_step": "起卦中...",
        "steps": [
            {"name": "bazi", "label": "八字起算", "icon": "☯", "status": "pending"},
            {"name": "dev-engineer", "label": "工位扫描", "icon": "🔍", "status": "pending"},
            {"name": "visual-designer", "label": "合断方案", "icon": "🎨", "status": "pending"},
            {"name": "project-director", "label": "终审呈判", "icon": "✓", "status": "pending"},
        ]
    }

def update_step(task_id: str, step_name: str, status: str, progress: int, label: str):
    if task_id in tasks:
        tasks[task_id]["current_step"] = label
        tasks[task_id]["progress"] = progress
        for step in tasks[task_id]["steps"]:
            if step["name"] == step_name:
                step["status"] = status


async def run_pipeline(task_id: str, photo_path: str, key: str, bazi_info: dict, retest_token: str = ""):
    """完整流水线：八字 + 工位扫描 + 合断 + 终审（内存存储版本）"""
    tasks[task_id]["status"] = "running"
    try:
        # 复测加成
        retest_bonus = 0
        prev_report = None
        if retest_token:
            if retest_token in IN_MEMORY_REPORTS:
                prev_report = IN_MEMORY_REPORTS[retest_token]
            retest_bonus = 20
            tasks[task_id]["is_retest"] = True

        # Step 1: 八字
        update_step(task_id, "bazi", "running", 15, "推演命主八字...")
        await asyncio.sleep(1.8)
        update_step(task_id, "bazi", "completed", 25, "八字已立")
        tasks[task_id]["bazi_info"] = bazi_info

        # Step 2: dev-engineer
        update_step(task_id, "dev-engineer", "running", 35, "扫描工位外境...")
        scan = await call_dev_engineer()
        update_step(task_id, "dev-engineer", "completed", 50, "扫描完成")
        tasks[task_id]["scan_result"] = scan

        # 复测时分数提升
        if retest_bonus > 0:
            scan["overall_score"] = min(95, scan["overall_score"] + retest_bonus)
            scan["risk_factors"] = [f for f in scan["risk_factors"] if f not in ["假花失气", "右侧过高"]]
            scan["advantages"] = ["已采纳建议改造", "气场明显改善"] + scan.get("advantages", [])

        # Step 3: visual-designer
        update_step(task_id, "visual-designer", "running", 65, "八字工位合断中...")
        design = await call_visual_designer(scan, bazi_info)
        if retest_bonus > 0:
            design["overview"]["benefit_summary"] = "复测可见气场已显著改善，各要素趋于平衡。建议持续维护，并逐步落实'本月'与'长期'建议项。"
        update_step(task_id, "visual-designer", "completed", 80, "合断方案已定")
        tasks[task_id]["design_result"] = design

        # Step 4: project-director
        update_step(task_id, "project-director", "running", 90, "终审呈判...")
        director = await call_project_director(scan, design, bazi_info)
        if retest_bonus > 0:
            director["final"]["summary"] = f"复测见格局由{prev_report['scan_result']['overall_score']}分升至{scan['overall_score']}分，气场显著改善。望持续养护，勿令反复。"
            director["final"]["top3"] = [
                "持续每周整理，保持明堂开阔",
                "每月初一检视绿植状态，及时更换",
                "下半年可视事业进展追加新摆件"
            ]
        update_step(task_id, "project-director", "completed", 100, "判毕")
        tasks[task_id]["final_report"] = director

        tasks[task_id]["status"] = "completed"
        use_key(key)

        # 保存报告到内存
        IN_MEMORY_REPORTS[task_id] = {
            "task_id": task_id,
            "scan_result": scan,
            "design_result": design,
            "final_report": director,
            "bazi_info": bazi_info,
            "is_retest": retest_bonus > 0,
            "prev_score": prev_report["scan_result"]["overall_score"] if prev_report else None,
            "current_score": scan["overall_score"],
            "unlocked": False,
            "disclaimer": "本判基于环境心理学与术数传统模型推演，仅供娱乐参考，请理性看待。"
        }
    except Exception as e:
        tasks[task_id]["status"] = "failed"
        tasks[task_id]["error"] = str(e)


# ============ 客户端 API ============
@app.post("/verify-key")
async def verify_key_endpoint(data: dict):
    key = data.get("key", "").strip().upper()
    if not key: raise HTTPException(status_code=400, detail="请输入密钥")
    result = verify_key(key)
    if not result["valid"]: raise HTTPException(status_code=403, detail=result["reason"])
    return {"valid": True}

@app.post("/upload")
async def upload_photo(
    file: UploadFile = File(...),
    x_key: str = Header(...),
    birthdate: str = Form(""),
    birthtime: str = Form(""),
    calendarType: str = Form("solar"),
    gender: str = Form("male"),
    name: str = Form(""),
    retest_token: str = Form("")
):
    result = verify_key(x_key.upper())
    if not result["valid"]: raise HTTPException(status_code=403, detail=result["reason"])
    if not file.content_type.startswith("image/"): raise HTTPException(status_code=400, detail="只支持图片")

    task_id = str(uuid.uuid4())[:8]
    file_path = f"{task_id}_{file.filename}"  # 内存存储，仅保留文件名

    # 计算八字
    bazi_info = calculate_bazi(birthdate, birthtime, gender, name)

    init_task(task_id)
    asyncio.create_task(run_pipeline(task_id, file_path, x_key.upper(), bazi_info, retest_token.upper()))

    return {"task_id": task_id}

@app.get("/status/{task_id}")
async def get_status(task_id: str):
    if task_id not in tasks: raise HTTPException(status_code=404, detail="任务不存在")
    return tasks[task_id]

@app.get("/report/{task_id}")
async def get_report(task_id: str, x_key: str = Header(...)):
    result = verify_key(x_key.upper())
    if not result["valid"]: raise HTTPException(status_code=403, detail=result["reason"])
    if task_id not in IN_MEMORY_REPORTS: raise HTTPException(status_code=404, detail="报告不存在")
    full_report = IN_MEMORY_REPORTS[task_id]

    # 检查是否已解锁付费内容
    unlocked = full_report.get("unlocked", False)
    if not unlocked:
        # 返回免费版：隐藏详细危害/好处、推荐物品、本月/长期建议
        locked_report = {
            "task_id": full_report["task_id"],
            "is_retest": full_report.get("is_retest", False),
            "prev_score": full_report.get("prev_score"),
            "current_score": full_report.get("current_score"),
            "bazi_info": full_report["bazi_info"],
            "scan_result": {
                "overall_score": full_report["scan_result"]["overall_score"],
                "risk_factors": full_report["scan_result"]["risk_factors"],
                "advantages": full_report["scan_result"]["advantages"],
                "seat_analysis": {"note": full_report["scan_result"]["seat_analysis"]["note"]},
                "desktop_analysis": {"note": full_report["scan_result"]["desktop_analysis"]["note"]},
                "plant_decoration": {"note": full_report["scan_result"]["plant_decoration"]["note"]},
                "overhead": {"note": full_report["scan_result"]["overhead"]["note"]},
                "lighting": {"note": full_report["scan_result"]["lighting"]["note"]},
                "background": {"note": full_report["scan_result"]["background"]["note"]},
            },
            "design_result": {
                "overview": {
                    "before": full_report["design_result"]["overview"]["before"],
                    "after": full_report["design_result"]["overview"]["after"],
                    "score": full_report["design_result"]["overview"]["score"],
                },
                "immediate_actions": full_report["design_result"].get("immediate_actions", []),
            },
            "final_report": {
                "overall": full_report["final_report"]["overall"],
                "final": {
                    "summary": full_report["final_report"]["final"]["summary"]
                }
            },
            "disclaimer": full_report["disclaimer"],
            "unlocked": False,
            "unlock_price": "9.9"
        }
        return locked_report
    return full_report


@app.get("/report-free/{task_id}")
async def get_report_free(task_id: str, x_key: str = Header(...)):
    """免费版报告接口：始终返回完整数据"""
    result = verify_key(x_key.upper())
    if not result["valid"]: raise HTTPException(status_code=403, detail=result["reason"])
    if task_id not in IN_MEMORY_REPORTS: raise HTTPException(status_code=404, detail="报告不存在")
    report = IN_MEMORY_REPORTS[task_id]
    report["unlocked"] = True
    report["free_version"] = True
    return report


@app.post("/unlock/{task_id}")
async def unlock_report(task_id: str, data: dict, x_key: str = Header(...)):
    """解锁付费内容"""
    result = verify_key(x_key.upper())
    if not result["valid"]: raise HTTPException(status_code=403, detail=result["reason"])

    if task_id not in IN_MEMORY_REPORTS: raise HTTPException(status_code=404, detail="报告不存在")

    payment_proof = data.get("payment_proof", "").strip()

    report = IN_MEMORY_REPORTS[task_id]

    if report.get("unlocked"):
        return {"unlocked": True, "message": "已解锁"}

    # 标记解锁（实际生产应验证支付凭证）
    report["unlocked"] = True
    report["unlocked_at"] = datetime.now().isoformat()
    report["unlock_proof"] = payment_proof or "manual_unlock"

    IN_MEMORY_REPORTS[task_id] = report

    return {"unlocked": True, "message": "解锁成功"}


@app.post("/admin/unlock/{task_id}")
async def admin_unlock(task_id: str, authorization: str = Header(None)):
    """后台手动解锁（验证支付后用）"""
    check_admin(authorization)
    if task_id not in IN_MEMORY_REPORTS: raise HTTPException(status_code=404, detail="报告不存在")
    report = IN_MEMORY_REPORTS[task_id]
    report["unlocked"] = True
    report["unlocked_at"] = datetime.now().isoformat()
    IN_MEMORY_REPORTS[task_id] = report
    return {"unlocked": True}

# ============ 后台 API ============
def check_admin(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未授权")
    if authorization.replace("Bearer ", "") != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="无效token")

@app.post("/admin/keys/create")
async def admin_create_keys(data: KeyCreate, authorization: str = Header(None)):
    check_admin(authorization)
    data_file = load_keys()
    new_keys = []
    for _ in range(data.count):
        key = generate_key()
        now = datetime.now()
        key_info = {
            "key": key, "created_at": now.isoformat(),
            "expires_at": (now + timedelta(days=data.days)).isoformat(),
            "used_count": 0, "note": data.note, "active": True
        }
        data_file["keys"].append(key_info)
        new_keys.append(key_info)
    await save_keys_remote(data_file)
    return {"created": len(new_keys), "keys": new_keys}

@app.get("/admin/keys/list")
async def admin_list_keys(authorization: str = Header(None)):
    check_admin(authorization)
    return load_keys()

@app.post("/admin/keys/toggle/{key}")
async def admin_toggle_key(key: str, authorization: str = Header(None)):
    check_admin(authorization)
    data = load_keys()
    for k in data["keys"]:
        if k["key"] == key.upper():
            k["active"] = not k["active"]
            await save_keys_remote(data)
            return {"key": key, "active": k["active"]}
    raise HTTPException(status_code=404, detail="密钥不存在")

@app.delete("/admin/keys/{key}")
async def admin_delete_key(key: str, authorization: str = Header(None)):
    check_admin(authorization)
    data = load_keys()
    data["keys"] = [k for k in data["keys"] if k["key"] != key.upper()]
    await save_keys_remote(data)
    return {"deleted": key}

@app.get("/admin/stats")
async def admin_stats(authorization: str = Header(None)):
    check_admin(authorization)
    data = load_keys()
    keys = data["keys"]
    now = datetime.now()
    return {
        "total": len(keys),
        "active": sum(1 for k in keys if k["active"] and datetime.fromisoformat(k["expires_at"]) > now),
        "expired": sum(1 for k in keys if datetime.fromisoformat(k["expires_at"]) <= now),
        "total_usage": sum(k["used_count"] for k in keys)
    }

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
