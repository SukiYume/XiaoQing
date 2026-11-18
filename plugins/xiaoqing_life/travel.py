"""按当地日期收录小青的旅行明信片。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from core.interfaces import PluginContextProtocol
from core.log import LogEvent

from .stores import life_data_directory

SEED        = Path(__file__).with_name("site") / "assets/travel"
TRAVEL_ZONE = ZoneInfo("America/Los_Angeles")
log         = logging.getLogger(__name__)


def travel_entries(root: Path) -> list[dict]:
    return _read_entries(life_data_directory(root) / "travel")


def _read_entries(directory: Path) -> list[dict]:
    entries = {}
    for folder, prefix in ((SEED, "/assets/travel"), (directory, "/travel-media")):
        for manifest in folder.glob("????-??-??/entry.json"):
            day = manifest.parent.name
            try:
                entry  = json.loads(manifest.read_bytes())
                photos = entry["photos"]
                if not (
                    isinstance(photos, list)
                    and 1 <= len(photos) <= 2
                    and all(
                        isinstance(entry[key], str) and entry[key].strip()
                        for key in ("country", "city")
                    )
                ):
                    raise ValueError("invalid travel entry")
                for n, photo in enumerate(photos, 1):
                    if not (
                        isinstance(photo["caption"], str)
                        and photo["caption"].strip()
                        and all(
                            type(photo[key]) is int and photo[key] > 0
                            for key in ("width", "height")
                        )
                    ):
                        raise ValueError("invalid travel photo")
                    photo["src"] = f"{prefix}/{day}/{n}.webp"
            except (OSError, ValueError, KeyError, TypeError) as exc:
                log.warning(LogEvent("travel.entry_unreadable", day=day, error=exc))
                continue
            entries[day] = {
                "date"   : day,
                "country": entry["country"],
                "city"   : entry["city"],
                "photos" : photos,
            }
    return [entries[day] for day in sorted(entries, reverse=True)]


def build_travel_prompt(day: date, recent: list[dict]) -> tuple[str, tuple[str, ...]]:
    """返回送给 Codex 的完整创作指令，以及按角色、场景、成片顺序附上的三张参考图。"""
    assets     = SEED.parent
    references = (
        assets / "travel-character.webp",
        assets / "library.webp",
        SEED / "2026-11-17/1.webp",
    )
    places = "、".join(f"{stop['country']}·{stop['city']}" for stop in recent[:20])
    prompt = f"""为天文系大二学生小青画 {day.isoformat()} 的世界旅行明信片。近期行程为{places or "首次出发"}。
挑一个近期行程之外的目的地，逐日走访不同地区。选取当地可辨认的景观和一件具体的小事。
用内置 GPT Image 生成一至两张独立的 16:9 横幅插画。以第一张附件为人物原设，沿用脸型、五官比例、
灰紫短发与侧边小辫、蓝眼睛、金色星星耳环。她随身带深蓝色观测笔记本，封面有金色土星。
第二张附件给出观测站的游戏插画风格，第三张给出旅行明信片的取景和质感。为新目的地重新构图。
为每一张图单独设计衣着和随身装备。根据目的地所在半球、当天所属季节、当地气候、地形、
画面里的早晚和活动，选择合适的外套或上衣、下装、鞋和实际用得上的装备。参考图里的校服属于校园场景。
两张图时选同一目的地的两个时段和活动，让换装、携带的器材、动作、光线和机位都有清楚的变化。
人物融入场景，用自然的动作表现她对当地事物的好奇。整幅画面铺满，无字、无边框、无水印。
逐张查看生成结果，用小青的口吻为每张图写一句具体的中文手记。把新图依次复制为
{day}-1.png 和可选的 {day}-2.png，放进本任务的图片产物目录。参考图仅用作输入。
最终回复先给出以下格式的 JSON，再按插件约定列出图片链接。captions 依文件编号排列。
{{"country":"国家或地区","city":"城市或地点","captions":["第一张图的手记"]}}"""
    return prompt, tuple(str(path.resolve(strict=True)) for path in references)


def already_published(directory: Path, day: date) -> bool:
    return any((folder / day.isoformat() / "entry.json").is_file() for folder in (SEED, directory))


async def generate_travel_day(context: PluginContextProtocol, day: date) -> None:
    directory = context.data_dir / "travel"
    try:
        if already_published(directory, day):
            return
        recent   = await asyncio.to_thread(_read_entries, directory)
        generate = context.capabilities.travel_postcards
        if generate is None:
            raise RuntimeError("旅行绘图服务未加载")
        result = await generate(*build_travel_prompt(day, recent))
        if result["status"] != "done":
            raise RuntimeError(f"Codex 任务状态 {result['status']}")
        text = result["final_text"]
        entry, _ = json.JSONDecoder().raw_decode(text[text.index("{") :])
        captions = entry["captions"]
        if not isinstance(captions, list) or len(captions) not in (1, 2):
            raise ValueError("旅行手记与图片数量应为一至两组")
        files = result["images_by_name"]
        names = [f"{day}-{n}.png" for n in (1, 2)]
        if [name for name in names if name in files] != names[: len(captions)]:
            raise ValueError("旅行图与手记编号不一致")
        photos = [
            (Path(files[name]), caption) for name, caption in zip(names, captions, strict=False)
        ]
        if await asyncio.to_thread(
            publish, directory, day, entry["country"], entry["city"], photos
        ):
            log.info(LogEvent("travel.published", day=day.isoformat(), photos=len(photos)))
    except Exception as exc:
        log.warning(LogEvent("travel.failed", day=day.isoformat(), error=exc), exc_info=True)


def publish(
    folder: Path, day: date, country: str, city: str, photos: list[tuple[Path, str]]
) -> bool:
    target = folder / day.isoformat()
    if already_published(folder, day):
        return False
    if not (
        country.strip()
        and city.strip()
        and 1 <= len(photos) <= 2
        and all(caption.strip() for _, caption in photos)
    ):
        raise ValueError("每站需要国家或地区、城市或地点，以及一至两张附有手记的图。")

    from PIL import Image, ImageOps

    folder.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=folder, prefix=".publish-") as temp:
        staged = Path(temp) / "postcards"
        staged.mkdir()
        cards = []
        for n, (source, caption) in enumerate(photos, 1):
            with Image.open(source) as original:
                img = ImageOps.exif_transpose(original).convert("RGB")
                img.thumbnail((1536, 1536), Image.LANCZOS)
                img.save(staged / f"{n}.webp", quality=88, method=6)
                cards.append({"caption": caption.strip(), "width": img.width, "height": img.height})
        manifest = {"country": country.strip(), "city": city.strip(), "photos": cards}
        (staged / "entry.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
        )
        try:
            staged.rename(target)
        except OSError:
            if (target / "entry.json").is_file():
                return False
            raise
    return True


def main() -> None:
    from main import _default_root

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=_default_root())
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--country", required=True)
    parser.add_argument("--city", required=True)
    parser.add_argument(
        "--photo", nargs=2, metavar=("IMAGE", "CAPTION"), action="append", required=True
    )
    args   = parser.parse_args()
    photos = [(Path(source), caption) for source, caption in args.photo]
    added  = publish(
        life_data_directory(args.root) / "travel", args.date, args.country, args.city, photos
    )
    print(f"{args.date} {'已收进旅行手帐' if added else '已收录'}")


if __name__ == "__main__":
    main()
