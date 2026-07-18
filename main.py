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
import random
import hashlib
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


# ============ 本地算法：工位扫描 / 合断方案 / 终审 ============
# 零成本实现：基于图片特征 + 八字五行 + 随机种子生成确定性结果
# 相同输入（同照片 + 同生日）=> 相同输出；不同输入 => 不同输出

# ---- 风险因素 / 优势 模板池 ----
_RISK_POOL = [
    "背门而坐", "横梁压顶", "桌面杂乱", "背光作业", "右白虎过高",
    "左青龙过低", "空调直吹", "假花失气", "明堂受阻", "光线不足",
    "背后无靠", "尖锐物品外露", "色彩过暗", "水景不当", "管线凌乱",
]
_ADV_POOL = [
    "采光充足", "背有实墙", "明堂开阔", "绿植生旺", "色彩和谐",
    "左高右低", "光线柔和", "桌面整洁", "气场流通", "方位得宜",
]

# ---- 各分析项文案模板池 ----
_SEAT_NOTES = [
    "座位朝向尚可，背后略有依靠", "座位背门而设，气场受冲",
    "座位面壁而坐，明堂逼仄", "座位临窗，采光虽佳然背空",
    "座位居中，左右尚称", "座位侧对走道，气口动荡",
]
_SEAT_HARMS = [
    "背门而坐，主犯小人，气运难聚。", "背空无靠，事业乏贵人扶持。",
    "面壁而坐，前途受阻，心胸易郁。", "门冲之气直射后脑，易生头痛失眠。",
    "侧对走道，气流紊乱，主心神不宁。",
]
_SEAT_BENEFITS = [
    "背有实墙，主得贵人相助，事业稳固。", "明堂开阔，前途光明，思路畅达。",
    "方位得宜，气场流通，身心安泰。", "左高右低，青龙得位，主升迁之象。",
]

_DESKTOP_NOTES = [
    "桌面略显凌乱，杂物堆积", "桌面整洁有序，物品归位",
    "桌面左右失衡，右侧偏高", "桌面物品适中，明堂尚可",
    "桌面拥挤，空间局促", "桌面左高右低，方位得宜",
]
_DESKTOP_HARMS = [
    "杂物堆积明堂，主思绪混乱，决策失误。", "右白虎过高，主阴盛阳衰，易招口舌。",
    "桌面拥挤逼仄，气场壅滞，运势难舒。", "物品无序，主心神不宁，效率低下。",
]
_DESKTOP_BENEFITS = [
    "桌面整洁，明堂开阔，主思路清晰。", "左高右低，青龙抬头，主事业顺遂。",
    "物品归位，气场流通，主心境平和。", "空间适度，主进退有据，条理分明。",
]

_PLANT_NOTES = [
    "未见绿植，生气不足", "绿植茂盛，生气盎然",
    "绿植略显枯黄，需养护", "有塑料假花，失其生气",
    "绿植摆放得当，点缀有方", "植物带刺，略有煞气",
]
_PLANT_HARMS = [
    "假花失气，主虚花无果，徒增浮躁。", "植物枯萎，主衰败之象，宜速更换。",
    "尖锐植物带煞，主口角是非。", "无绿植生气，气场沉滞，缺乏生机。",
]
_PLANT_BENEFITS = [
    "绿植生旺，主化煞添生气，利文昌。", "植物茂盛，主生机勃勃，事业兴旺。",
    "摆放得当，主聚气藏风，财气渐聚。",
]

_OVERHEAD_NOTES = [
    "头顶未见横梁，环境尚可", "疑似横梁压顶，宜化解",
    "灯具直射头顶，光线过强", "空调直吹头部，易生不适",
    "头顶环境整洁，无压迫", "头顶管线外露，略有杂乱",
]
_OVERHEAD_HARMS = [
    "横梁压顶，主压力重重，头疾易生。", "灯煞直射，主心神不宁，目疾易发。",
    "空调直吹，主风邪入体，肩颈酸痛。",
]
_OVERHEAD_BENEFITS = [
    "头顶开阔，主心胸舒畅，思维敏捷。", "无梁无煞，主安居无忧，气场平和。",
    "光线柔和，主目明神清，效率提升。",
]

_LIGHTING_NOTES = [
    "光线适中，明暗得宜", "光线偏暗，需补充光源",
    "光线过强，略觉刺眼", "背光作业，目力受损",
    "自然采光，光线柔和", "光源单一，明暗不均",
]
_LIGHTING_HARMS = [
    "光线昏暗，主阳气不足，精神萎靡。", "背光作业，主目力受损，判断失误。",
    "强光直射，主心浮气躁，难以专注。",
]
_LIGHTING_BENEFITS = [
    "光线柔和充足，主阳气充沛，神清气爽。", "自然采光，主气场清明，思路畅达。",
    "明暗得宜，主心境平和，效率提升。",
]

_BACKGROUND_NOTES = [
    "背景墙面素净，无特殊装饰", "背景色彩偏暗，略显压抑",
    "背景有装饰画，点缀得当", "背景杂乱，物品过多",
    "背景色调温和，气场和谐", "背景有空镜，反光扰气",
]
_BACKGROUND_HARMS = [
    "背景过暗，主阴气过重，运势低迷。", "杂物过多，主气场混乱，心绪不宁。",
    "色彩冲克，主五行失衡，诸事不顺。",
]
_BACKGROUND_BENEFITS = [
    "背景素净，主气场清明，心无杂念。", "装饰得当，主雅致生辉，文昌得利。",
    "色调和谐，主五行相生，运势平稳。",
]

_PHOTO_QUALITY = ["清晰", "清晰", "模糊", "角度不佳", "清晰", "尚可"]

# ---- 五行 -> 推荐物品映射（喜用神决定推荐物品）----
PLANT_MAP = {
    "木": [
        ("绿萝", "桌面左侧青龙位", "补木生气，活化气场", "15-25元", "小型盆栽（高度15-25cm），带盆托"),
        ("文竹", "桌面左前", "木气生发，利文昌", "10-20元", "小巧文竹一盆"),
        ("富贵竹", "桌面右侧", "木水相生，旺财气", "8-15元", "水培3-5支"),
    ],
    "水": [
        ("富贵竹", "桌面水培位", "水木相生，旺财运", "8-15元", "水培3-5支"),
        ("小型鱼缸", "桌面左前", "活水聚财，生气流转", "30-60元", "直径15cm小型鱼缸"),
        ("水培铜钱草", "桌面明堂位", "水气润局，招财纳福", "10-18元", "水培小杯"),
    ],
    "金": [
        ("铜葫芦", "桌面抽屉内", "金气化煞，镇宅护身", "20-40元", "小铜葫芦一只"),
        ("金属笔筒", "桌面右侧", "金气助运，理顺文书", "15-30元", "金属笔筒"),
        ("白水晶", "桌面左前", "金气清明，提升决断", "20-35元", "白水晶簇小块"),
    ],
    "火": [
        ("红掌", "桌面明堂位", "火气生旺，添喜庆", "15-30元", "红掌小盆"),
        ("朱蕉", "桌面左侧", "火木相生，旺事业", "20-40元", "朱蕉小株"),
        ("红色摆件", "桌面右后", "火气助运，提升人气", "10-25元", "红色小摆件"),
    ],
    "土": [
        ("虎皮兰", "桌面左后", "土气稳固，镇宅聚财", "20-40元", "虎皮兰小盆"),
        ("黄玉摆件", "桌面明堂", "土气生金，旺财运", "25-50元", "黄玉小摆件"),
        ("陶瓷花器", "桌面右侧", "土气中和，平衡五行", "15-30元", "陶瓷小花瓶"),
    ],
}

# ---- 即时行动 / 长期建议 模板池 ----
_IMMEDIATE_TEMPLATES = [
    "清理桌面杂物，保持明堂开阔", "调整座椅朝向，避免背门而坐",
    "整理管线，理顺桌面气场", "移除尖锐物品，化解煞气",
    "调整灯光角度，避免直射", "摆放绿植于青龙位，添生气",
    "清理背后杂物，稳固靠山", "擦拭桌面，保持洁净明亮",
]
_LONG_TERM_TEMPLATES = [
    "每周五下班前清理桌面，保持明堂开阔", "绿植每周浇水，枯叶即剪，勿使衰败",
    "每月初一检视布局，依气场微调", "每季度更换绿植，保持生气常新",
    "定期整理文件，勿使杂物堆积", "保持座椅靠墙，勿随意挪动",
    "每半年清洁灯具，保持光线明亮", "依节气调整摆件方位，顺应天时",
]

# ---- 终审结语模板池 ----
_CLOSINGS = [
    "格局可改，气运可调。命由天定，运由己造。",
    "风水之妙，在于调和。持之以恒，福泽自至。",
    "一命二运三风水，调候得宜，自可转祸为福。",
    "方位既正，气场自和。心正则气正，气正则运昌。",
]


def _pick(rng: random.Random, pool: list, n: int) -> list:
    """从池中随机选 n 个不重复元素（不足则全取）"""
    if n <= 0:
        return []
    if n >= len(pool):
        return list(pool)
    return rng.sample(pool, n)


def _extract_image_features(photo_path: str) -> dict:
    """提取图片特征用于生成种子与分析（PIL 不可用时退化为文件大小+文件名哈希）"""
    features = {
        "file_size": 0, "file_hash": "0", "brightness": 0.5,
        "dominant_color": "neutral", "has_green": False, "has_warm": False,
        "width": 0, "height": 0,
    }
    # 文件大小 + 哈希（始终可用）
    try:
        with open(photo_path, "rb") as f:
            file_data = f.read()
        features["file_size"] = len(file_data)
        features["file_hash"] = hashlib.md5(file_data).hexdigest()
    except Exception:
        features["file_hash"] = hashlib.md5(photo_path.encode()).hexdigest()

    # PIL 提取像素特征（可选，不可用则跳过）
    try:
        from PIL import Image
        img = Image.open(photo_path)
        img = img.convert("RGB")
        features["width"], features["height"] = img.size
        img.thumbnail((120, 120))
        pixels = list(img.getdata())
        if pixels:
            total = len(pixels)
            avg_r = sum(p[0] for p in pixels) / total
            avg_g = sum(p[1] for p in pixels) / total
            avg_b = sum(p[2] for p in pixels) / total
            features["brightness"] = (avg_r + avg_g + avg_b) / 3 / 255.0
            if avg_g > avg_r + 10 and avg_g > avg_b + 10:
                features["dominant_color"] = "green"
                features["has_green"] = True
            elif avg_r > avg_g + 15 and avg_r > avg_b:
                features["dominant_color"] = "red"
                features["has_warm"] = True
            elif avg_b > avg_r and avg_b > avg_g + 10:
                features["dominant_color"] = "blue"
            else:
                features["dominant_color"] = "neutral"
    except Exception as e:
        print(f"PIL 不可用或图片读取失败，使用文件特征做种子: {e}")

    return features


async def call_dev_engineer(photo_path: str, bazi_info: dict = None) -> dict:
    """工位扫描 - 本地算法生成（零成本，基于图片特征 + 八字 + 随机种子）"""
    features = _extract_image_features(photo_path)

    # 种子 = 图片特征 + 八字信息（不同生日=不同分数）
    bazi_seed = ""
    if bazi_info:
        bazi_seed = f":{bazi_info.get('birthdate', '')}:{bazi_info.get('birthtime', '')}:{bazi_info.get('gender', '')}"
    seed_str = (
        f"{features['file_hash']}:{features['file_size']}:"
        f"{features['brightness']:.4f}:{features['dominant_color']}:"
        f"{features['width']}x{features['height']}{bazi_seed}"
    )
    seed = int(hashlib.md5(seed_str.encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)

    await asyncio.sleep(0.2)

    # 综合评分（35-85），受图片特征 + 八字五行影响
    base = rng.randint(35, 85)
    if features["brightness"] < 0.3:
        base -= 6
    elif features["brightness"] > 0.75:
        base += 4
    if features["has_green"]:
        base += 5
    if features["has_warm"]:
        base += 2
    # 八字五行直接影响分数（不同生日 = 不同分数）
    if bazi_info:
        wuxing = bazi_info.get("wuxing", {})
        day_master = bazi_info.get("day_master", "")
        # 根据日主五行和五行平衡度调整分数
        wuxing_values = list(wuxing.values()) if wuxing else [1,1,1,1,1]
        wuxing_range = max(wuxing_values) - min(wuxing_values) if wuxing_values else 0
        # 五行越平衡分数越高
        base += max(0, 8 - wuxing_range * 2)
        # 日主五行与图片主色调的生克关系
        dm_wuxing = {"甲":"mu","乙":"mu","丙":"huo","丁":"huo","戊":"tu","己":"tu","庚":"jin","辛":"jin","壬":"shui","癸":"shui"}.get(day_master, "")
        if dm_wuxing == "mu" and features["has_green"]:
            base += 5
        elif dm_wuxing == "huo" and features["has_warm"]:
            base += 5
        elif dm_wuxing == "tu" and features["dominant_color"] == "neutral":
            base += 3
        elif dm_wuxing == "jin" and features["brightness"] > 0.6:
            base += 4
        elif dm_wuxing == "shui" and features["brightness"] < 0.4:
            base += 4
    overall_score = max(35, min(85, base))

    # 座位分析
    back_to = rng.choice(["背对实墙", "背对走廊", "背对窗户", "背对门", "背空"])
    seat = {
        "facing": rng.choice(["面朝窗户", "面朝墙壁", "面朝走廊", "面朝门", "面朝同事"]),
        "back_to": back_to,
        "has_support": "实墙" in back_to,
        "has_rush": ("走廊" in back_to) or ("门" in back_to),
        "note": rng.choice(_SEAT_NOTES),
        "harm": rng.choice(_SEAT_HARMS),
        "benefit": rng.choice(_SEAT_BENEFITS),
    }

    # 桌面分析
    desktop = {
        "items": rng.sample(
            ["笔记本电脑", "显示器", "文件堆", "水杯", "手机", "键盘", "鼠标", "台历", "笔筒", "绿植"],
            rng.randint(3, 6),
        ),
        "left_right": rng.choice(["右侧明显高于左侧", "左侧略高", "基本平衡", "左侧明显高于右侧"]),
        "clutter_score": rng.randint(2, 9),
        "note": rng.choice(_DESKTOP_NOTES),
        "harm": rng.choice(_DESKTOP_HARMS),
        "benefit": rng.choice(_DESKTOP_BENEFITS),
    }

    # 植物装饰
    has_plant = features["has_green"] or rng.random() > 0.5
    plant = {
        "has_plant": has_plant,
        "plant_type": rng.choice(["绿萝", "多肉", "文竹", "富贵竹", "塑料假花"]) if has_plant else "无",
        "plant_status": rng.choice(["茂盛", "略黄", "枯萎", "良好"]) if has_plant else "无",
        "sharp_objects": _pick(rng, ["剪刀", "美工刀", "仙人掌", "金属笔", "无"], rng.randint(0, 2)),
        "water_feature": rng.random() > 0.85,
        "note": rng.choice(_PLANT_NOTES),
        "harm": rng.choice(_PLANT_HARMS),
        "benefit": rng.choice(_PLANT_BENEFITS),
    }
    if not plant["sharp_objects"]:
        plant["sharp_objects"] = ["无"]

    # 头顶环境
    overhead = {
        "beam": rng.random() > 0.7,
        "light_type": rng.choice(["长条形LED灯", "吊灯", "吸顶灯", "台灯", "无"]),
        "light_direct": rng.random() > 0.6,
        "air_con": rng.random() > 0.4,
        "air_con_direct": rng.random() > 0.7,
        "note": rng.choice(_OVERHEAD_NOTES),
        "harm": rng.choice(_OVERHEAD_HARMS),
        "benefit": rng.choice(_OVERHEAD_BENEFITS),
    }

    # 光线（亮度受图片特征影响）
    if features["brightness"] < 0.3:
        brightness_label = rng.choice(["偏暗", "昏暗"])
    elif features["brightness"] > 0.75:
        brightness_label = rng.choice(["过亮", "柔和"])
    else:
        brightness_label = rng.choice(["偏暗", "适中", "过亮", "柔和"])
    lighting = {
        "source": rng.choice(["自然光", "混合光", "日光灯", "LED灯", "台灯"]),
        "brightness": brightness_label,
        "backlight": rng.random() > 0.7,
        "note": rng.choice(_LIGHTING_NOTES),
        "harm": rng.choice(_LIGHTING_HARMS),
        "benefit": rng.choice(_LIGHTING_BENEFITS),
    }

    # 背景
    background = {
        "color": rng.choice(["白色", "米色", "浅灰", "深蓝", "浅绿", "木色", "深色"]),
        "decorations": _pick(rng, ["装饰画", "照片墙", "挂钟", "置物架", "无"], rng.randint(1, 3)),
        "special": _pick(rng, ["镜子", "玻璃门", "窗户", "柱子", "无"], rng.randint(1, 2)),
        "note": rng.choice(_BACKGROUND_NOTES),
        "harm": rng.choice(_BACKGROUND_HARMS),
        "benefit": rng.choice(_BACKGROUND_BENEFITS),
    }
    if not background["decorations"]:
        background["decorations"] = ["无"]
    if not background["special"]:
        background["special"] = ["无"]

    risk_factors = _pick(rng, _RISK_POOL, rng.randint(2, 4))
    advantages = _pick(rng, _ADV_POOL, rng.randint(1, 3))

    return {
        "photo_quality": rng.choice(_PHOTO_QUALITY),
        "seat_analysis": seat,
        "desktop_analysis": desktop,
        "plant_decoration": plant,
        "overhead": overhead,
        "lighting": lighting,
        "background": background,
        "overall_score": overall_score,
        "risk_factors": risk_factors,
        "advantages": advantages,
    }


async def call_visual_designer(scan: dict, bazi: dict) -> dict:
    """合断方案 - 本地算法生成（基于八字五行喜用神 + 工位扫描问题）"""
    favorable = bazi.get("favorable", "金")
    # 种子 = 八字 + 扫描结果（确定性：相同输入相同输出，不同输入不同输出）
    seed_str = json.dumps(bazi, ensure_ascii=False, sort_keys=True) + "::" + \
               json.dumps(scan, ensure_ascii=False, sort_keys=True)
    seed = int(hashlib.md5(seed_str.encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)

    await asyncio.sleep(0.2)

    scan_score = scan.get("overall_score", 50)
    risk_factors = scan.get("risk_factors", [])

    # 喜用神对应物品（this_week 3 个）
    plant_list = PLANT_MAP.get(favorable, PLANT_MAP["金"])
    this_week = []
    for i in range(3):
        item = plant_list[i % len(plant_list)]
        this_week.append({
            "name": item[0], "location": item[1], "purpose": item[2],
            "price": item[3], "spec": item[4],
        })

    # this_month 2 个：从其他五行取互补物品
    other_keys = [k for k in PLANT_MAP.keys() if k != favorable]
    this_month = []
    used = set()
    while len(this_month) < 2 and other_keys:
        k = rng.choice(other_keys)
        if k not in used:
            used.add(k)
            item = rng.choice(PLANT_MAP[k])
            this_month.append({
                "name": item[0], "location": item[1], "purpose": item[2],
                "price": item[3], "spec": item[4],
            })

    # 即时行动 / 长期建议
    immediate_actions = _pick(rng, _IMMEDIATE_TEMPLATES, 3)
    long_term = _pick(rng, _LONG_TERM_TEMPLATES, 4)

    # overview
    if risk_factors:
        before = f"当前工位存在{'、'.join(risk_factors[:2])}等问题，气场有失"
    else:
        before = scan.get("seat_analysis", {}).get("note", "工位现状待改善")
    after = f"依命主喜用神【{favorable}】调候，补益{favorable}气，可令气场趋于平衡"
    design_score = round(min(9.5, max(6.0, scan_score / 10 + 1.5 + rng.uniform(-0.5, 0.5))), 1)

    # harm / benefit 文案
    risk_text = risk_factors[0] if risk_factors else "气场失衡"
    harm_opts = ["运势低迷", "口舌是非", "头疾失眠", "决策失误", "贵人难逢", "思绪混乱"]
    harm_word = rng.choice(harm_opts)
    harm_summary = rng.choice([
        f"工位现状{risk_text}，气场壅滞。若不调候，恐生{harm_word}之患，久则运势渐衰。",
        f"命主喜{favorable}气，工位又逢{risk_text}，两相冲克。不改则{harm_word}，事业难进。",
        f"明堂受困，{risk_text}未化，加以命局喜{favorable}而不得，长此以往{harm_word}。",
    ])
    benefit_summary = rng.choice([
        f"依命主喜{favorable}之理，添置{favorable}气之物，可令气场流转，化煞生旺，事业渐入佳境。",
        f"调候得宜，{favorable}气得补，明堂开朗，主贵人扶持，运势稳步上扬。",
        f"格局既正，{favorable}气归位，煞气化解，主身心安泰，诸事顺遂。",
        f"五行既调，{favorable}气充沛，气场清明，主文昌得利，决断分明。",
    ])

    # AI 配图提示词（英文）
    style_map = {
        "金": "metallic accents, white and gold tones",
        "木": "lush green plants, wooden textures",
        "水": "water features, blue and glass elements",
        "火": "warm red accents, vibrant lighting",
        "土": "earthy ceramics, yellow and beige tones",
    }
    style = style_map.get(favorable, "balanced natural elements")
    ai_prompt = f"A modern minimalist Chinese style office desk with feng shui layout, {style}, natural light, photorealistic, 8k --ar 16:9"

    # 布局简图
    layout = rng.choice([
        """    [窗户 - 自然光源]
    +------------------------+
    | [绿植]  [显示器]  [摆件] |
    |          [键盘]           |
    |  [水杯]   [鼠标] [香薰]   |
    |         <- 明堂 ->         |
    +------------------------+
            [背有实墙]
    """,
        """    [墙面 - 靠山]
    +------------------------+
    | [摆件]  [显示器]  [绿植] |
    |   [文件]  [键盘]  [水杯]  |
    |        <- 明堂 ->          |
    +------------------------+
        [走道 - 气场流通]
    """,
        """    [自然光 - 左侧入]
    +------------------------+
    | [绿植]  [显示器]        |
    |          [键盘]  [摆件]  |
    |  [水杯]   [鼠标]         |
    |         <- 明堂 ->         |
    +------------------------+
            [背有实墙]
    """,
    ])

    return {
        "overview": {
            "before": before,
            "after": after,
            "score": design_score,
            "harm_summary": harm_summary,
            "benefit_summary": benefit_summary,
        },
        "immediate_actions": immediate_actions,
        "this_week": this_week,
        "this_month": this_month,
        "long_term": long_term,
        "ai_prompt": ai_prompt,
        "layout": layout,
    }


async def call_project_director(scan: dict, design: dict, bazi: dict) -> dict:
    """终审呈判 - 本地算法生成（基于扫描分数 + 八字喜用神）"""
    favorable = bazi.get("favorable", "金")
    day_master = bazi.get("day_master", "")
    seed_str = json.dumps({"s": scan, "d": design, "b": bazi}, ensure_ascii=False, sort_keys=True)
    seed = int(hashlib.md5(seed_str.encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)

    await asyncio.sleep(0.2)

    scan_score = scan.get("overall_score", 50)
    risk_factors = scan.get("risk_factors", [])
    advantages = scan.get("advantages", [])

    # 改造后预期评分（60-95）
    final_score = int(min(95, max(60, scan_score + 15 + rng.randint(0, 8))))
    if final_score >= 85:
        status = "优秀"
    elif final_score >= 75:
        status = "良好"
    elif final_score >= 65:
        status = "通过"
    else:
        status = "待改进"

    # summary：按分数段选择文案（低分/中分/高分不同文案）
    risk_text = "、".join(risk_factors[:2]) if risk_factors else "气场失衡"
    adv_text = "、".join(advantages[:2]) if advantages else "方位得宜"
    if scan_score < 50:
        summary = rng.choice([
            f"命主日主{day_master}，喜用神【{favorable}】。工位现状堪忧，{risk_text}，煞气未化。所幸方案对症下药，补{favorable}气以调候，假以时日可转危为安。",
            f"观此工位，{risk_text}俱在，气场壅滞。然命主喜{favorable}，方案循五行之理布局，持之以恒，运势可望回升。",
        ])
    elif scan_score < 70:
        summary = rng.choice([
            f"命主日主{day_master}，喜【{favorable}】。工位虽有小疵，{risk_text}，然大体尚可。方案补{favorable}气、化微煞，格局渐入佳境。",
            f"工位现状中等，{risk_text}。依命主喜{favorable}之理调候，扬长避短，运势可稳步上扬。",
        ])
    else:
        summary = rng.choice([
            f"命主日主{day_master}，喜【{favorable}】。工位格局甚佳，{adv_text}。方案锦上添花，补{favorable}气以固本，主事业兴旺。",
            f"观此工位，明堂开阔，{adv_text}。命主喜{favorable}，方案循此布局，可保运势亨通，贵人常临。",
        ])

    # top3：结合风险因素与方案物品
    this_week_items = design.get("this_week", [])
    week_action = (
        f"本周做：购{this_week_items[0].get('name', '绿植')}置于{this_week_items[0].get('location', '青龙位')}，补{favorable}气"
        if this_week_items else f"本周做：添置{favorable}气之物于青龙位"
    )
    if risk_factors:
        top3 = [
            f"立即做：化解{risk_factors[0]}，调整座位或摆件方位",
            week_action,
            f"长期养：每周整理，每月检视，依{favorable}气调候勿使反复",
        ]
    else:
        top3 = [
            "立即做：清理桌面，保持明堂开阔",
            week_action,
            f"长期养：每周整理，每月检视，依{favorable}气调候勿使反复",
        ]

    closing = rng.choice(_CLOSINGS)

    return {
        "overall": {"status": status, "score": final_score},
        "final": {
            "summary": summary,
            "top3": top3,
            "closing": closing,
        },
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


async def run_pipeline(task_id: str, photo_path: str, key: str, bazi_info: dict):
    """完整流水线：八字 + 工位扫描 + 合断 + 终审（内存存储版本）"""
    tasks[task_id]["status"] = "running"
    try:
        prev_report = None

        # Step 1: 八字（复测时跳过，直接用之前的）
        update_step(task_id, "bazi", "running", 15, "推演命主八字...")
        await asyncio.sleep(0.5)
        update_step(task_id, "bazi", "completed", 25, "八字已立")
        tasks[task_id]["bazi_info"] = bazi_info

        # Step 2: dev-engineer
        update_step(task_id, "dev-engineer", "running", 35, "扫描工位外境...")
        scan = await call_dev_engineer(photo_path, bazi_info)
        update_step(task_id, "dev-engineer", "completed", 50, "扫描完成")
        tasks[task_id]["scan_result"] = scan



        # Step 3: visual-designer
        update_step(task_id, "visual-designer", "running", 65, "八字工位合断中...")
        design = await call_visual_designer(scan, bazi_info)
        update_step(task_id, "visual-designer", "completed", 80, "合断方案已定")
        tasks[task_id]["design_result"] = design

        # Step 4: project-director
        update_step(task_id, "project-director", "running", 90, "终审呈判...")
        director = await call_project_director(scan, design, bazi_info)
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
    name: str = Form("")
):
    result = verify_key(x_key.upper())
    if not result["valid"]: raise HTTPException(status_code=403, detail=result["reason"])
    if not file.content_type.startswith("image/"): raise HTTPException(status_code=400, detail="只支持图片")

    task_id = str(uuid.uuid4())[:8]
    # Vercel 只允许写 /tmp
    upload_dir = Path("/tmp/desksage_uploads")
    upload_dir.mkdir(parents=True, exist_ok=True)
    file_path = upload_dir / f"{task_id}_{file.filename}"
    with open(file_path, "wb") as f:
        f.write(await file.read())

    # 计算八字
    if birthdate and birthtime:
        bazi_info = calculate_bazi(birthdate, birthtime, gender, name)
    else:
        bazi_info = {"name": "", "birthdate": "", "birthtime": "", "gender": "", "bazi": [], "day_master": "", "wuxing_count": {}, "favorable": "", "synthesis": ""}

    init_task(task_id)
    # 同步执行流水线（Vercel serverless 后台任务会被冻结）
    await run_pipeline(task_id, str(file_path), x_key.upper(), bazi_info)

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
