"""Opt-in, independent Fcitx Pinyin packs; never opens personal learning data."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unicodedata
import uuid

MAX_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 50000
SYLLABLES = set("""a ai an ang ao ba bai ban bang bao bei ben beng bi bian biao bie bin bing bo bu
ca cai can cang cao ce cen ceng cha chai chan chang chao che chen cheng chi chong chou chu chua chuai chuan chuang chui chun chuo ci cong cou cu cuan cui cun cuo
da dai dan dang dao de dei den deng di dia dian diao die ding diu dong dou du duan dui dun duo
e ei en eng er fa fan fang fei fen feng fo fou fu
ga gai gan gang gao ge gei gen geng gong gou gu gua guai guan guang gui gun guo
ha hai han hang hao he hei hen heng hong hou hu hua huai huan huang hui hun huo
ji jia jian jiang jiao jie jin jing jiong jiu ju juan jue jun
ka kai kan kang kao ke kei ken keng kong kou ku kua kuai kuan kuang kui kun kuo
la lai lan lang lao le lei leng li lia lian liang liao lie lin ling liu lo long lou lu luan lun luo lv lve
ma mai man mang mao me mei men meng mi mian miao mie min ming miu mo mou mu
na nai nan nang nao ne nei nen neng ni nian niang niao nie nin ning niu nong nou nu nuan nuo nv nve
o ou pa pai pan pang pao pei pen peng pi pian piao pie pin ping po pou pu
qi qia qian qiang qiao qie qin qing qiong qiu qu quan que qun
ran rang rao re ren reng ri rong rou ru rua ruan rui run ruo
sa sai san sang sao se sen seng sha shai shan shang shao she shei shen sheng shi shou shu shua shuai shuan shuang shui shun shuo si song sou su suan sui sun suo
ta tai tan tang tao te teng ti tian tiao tie ting tong tou tu tuan tui tun tuo
wa wai wan wang wei wen weng wo wu xi xia xian xiang xiao xie xin xing xiong xiu xu xuan xue xun
ya yan yang yao ye yi yin ying yo yong you yu yuan yue yun
za zai zan zang zao ze zei zen zeng zha zhai zhan zhang zhao zhe zhei zhen zheng zhi zhong zhou zhu zhua zhuai zhuan zhuang zhui zhun zhuo zi zong zou zu zuan zui zun zuo m n ng hm hng""".split())


class DictionaryError(ValueError):
    """An actionable validation/import error suitable for display in settings."""


def _entry(word, pinyin, score="0", english="", line=0):
    prefix = f"第 {line} 行：" if line else ""
    word, pinyin, english = word.strip(), pinyin.strip().lower().replace("ü", "v"), english.strip()
    if not word or len(word) > 128 or any(c.isspace() or unicodedata.category(c).startswith("C") for c in word):
        raise DictionaryError(prefix + "词语必须为 1–128 个字符，且不能含空白或控制字符。")
    parts = pinyin.split("'")
    if len(parts) > 128 or any(p not in SYLLABLES for p in parts):
        raise DictionaryError(prefix + "拼音必须为无声调的完整音节，以英文单引号分隔，例如 hou'liang'zi。")
    if len(word) != len(parts):
        raise DictionaryError(prefix + "本版词包要求每个词语字符对应一个拼音音节；英文缩写请放在 english 列。")
    if not re.fullmatch(r"[0-9]{1,10}", str(score).strip()) or int(score) > 1000000000:
        raise DictionaryError(prefix + "score 必须为 0–1000000000 的整数。")
    if len(english) > 1024 or any(unicodedata.category(c).startswith("C") for c in english):
        raise DictionaryError(prefix + "英文释义过长或含控制字符。")
    return {"word": word, "pinyin": pinyin, "score": int(score), "english": english}


def _parse(path, include_digest=False):
    path = Path(path)
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise DictionaryError("词包不能超过 8 MiB。")
        content = raw.decode("utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise DictionaryError(f"无法读取 UTF-8 词包：{exc}") from exc
    entries = []
    seen = set()
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv"):
        reader = csv.DictReader(io.StringIO(content, newline=""), delimiter="," if suffix == ".csv" else "\t", strict=True)
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)) or not {"word", "pinyin"}.issubset(fields) or set(fields) - {"word", "pinyin", "score", "english"}:
            raise DictionaryError("表头需要 word,pinyin，可选 score,english，不接受重复或未知列。")
        try:
            for row in reader:
                if None in row or any(v is None for v in row.values()):
                    raise DictionaryError(f"第 {reader.line_num} 行列数不匹配。")
                entries.append(_entry(row["word"], row["pinyin"], row.get("score", "0"), row.get("english", ""), reader.line_num))
                if len(entries) > MAX_ENTRIES:
                    raise DictionaryError("词包不能超过 50000 条。")
        except csv.Error as exc:
            raise DictionaryError(f"CSV/TSV 格式错误：{exc}") from exc
    elif suffix == ".txt":
        for number, line in enumerate(content.splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            fields = line.split()
            if len(fields) != 3:
                raise DictionaryError(f"第 {number} 行需要：词语 拼音 score。")
            entries.append(_entry(*fields, line=number))
            if len(entries) > MAX_ENTRIES:
                raise DictionaryError("词包不能超过 50000 条。")
    else:
        raise DictionaryError("仅接受 UTF-8 .txt、.csv、.tsv 文件。")
    if not entries:
        raise DictionaryError("词包不能为空。")
    for entry in entries:
        key = entry["word"], entry["pinyin"]
        if key in seen:
            raise DictionaryError(f"重复词条：{entry['word']} / {entry['pinyin']}；请合并后重试。")
        seen.add(key)
    return (entries, hashlib.sha256(raw).hexdigest()) if include_digest else entries


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class DictionaryManager:
    def __init__(self, data_home: Path):
        self.data_home = Path(data_home)
        self.store = self.data_home / "transime" / "dictionaries"
        self.target = self.data_home / "pinyin" / "dictionaries"

    def _safe_dirs(self, create=False):
        if self.data_home.is_symlink():
            raise DictionaryError(f"拒绝使用符号链接数据目录：{self.data_home}")
        for directory in (self.store, self.target):
            for parent in (directory.parent, directory):
                if parent.is_symlink():
                    raise DictionaryError(f"拒绝使用符号链接目录：{parent}")
            if create:
                directory.mkdir(parents=True, exist_ok=True)

    def preview_file(self, path):
        entries, digest = _parse(path, include_digest=True)
        return {"count": len(entries), "entries": entries, "sha256": digest, "translator_integration": False,
                "notice": "英文释义仅保存与导出，尚不参与翻译。"}

    def import_file(self, path, name, expected_sha256=None):
        if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(unicodedata.category(c).startswith("C") for c in name):
            raise DictionaryError("词包名称需要 1–100 个字符且不含控制字符。")
        report = self.preview_file(path)
        if expected_sha256 is not None and report["sha256"] != expected_sha256:
            raise DictionaryError("词包文件在预览后发生变化；请重新预览再导入。")
        utility = shutil.which("libime_pinyindict")
        if utility is None:
            raise DictionaryError("未找到 libime_pinyindict；请先在该环境安装 LibIME 拼音词典工具。未导入任何词条。")
        self._safe_dirs(create=True)
        pack_id = uuid.uuid4().hex
        destination = self.target / f"transime-{pack_id}.dict"
        final_store = self.store / pack_id
        entries = report["entries"]
        with tempfile.TemporaryDirectory(prefix=".import-", dir=self.store) as stage_name:
            stage = Path(stage_name)
            source = stage / "source.tsv"
            with source.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["word", "pinyin", "score", "english"], delimiter="\t")
                writer.writeheader()
                writer.writerows(entries)
            plain, compiled, decoded = stage / "input.txt", stage / "compiled.dict", stage / "decoded.txt"
            plain.write_text("".join(f"{e['word']} {e['pinyin']} {e['score']}\n" for e in entries), encoding="utf-8")
            try:
                for args in ([utility, str(plain), str(compiled)], [utility, "-d", str(compiled), str(decoded)]):
                    result = subprocess.run(args, capture_output=True, text=True, timeout=60, check=False)
                    if result.returncode:
                        raise DictionaryError(f"拼音词典工具失败（退出码 {result.returncode}）；未导入。")
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise DictionaryError(f"无法编译或回读词包：{exc}") from exc
            actual = {(e["word"], e["pinyin"], e["score"]) for e in _parse(decoded)}
            expected = {(e["word"], e["pinyin"], e["score"]) for e in entries}
            if actual != expected or not compiled.is_file() or compiled.stat().st_size == 0:
                raise DictionaryError("编译回读与原词条不一致；未导入。")
            meta = {"schema": 1, "id": pack_id, "name": name.strip(), "count": len(entries),
                    "sha256": _sha(compiled), "source_sha256": _sha(source), "translator_integration": False}
            (stage / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            plain.unlink()
            decoded.unlink()
            # Stage the binary on its destination filesystem before atomically publishing.
            with tempfile.NamedTemporaryFile(prefix=".transime-", dir=self.target, delete=False) as stream:
                temporary_binary = Path(stream.name)
                stream.write(compiled.read_bytes())
            try:
                compiled.unlink()
                os.replace(stage, final_store)
                try:
                    os.replace(temporary_binary, destination)
                except OSError:
                    (final_store / "source.tsv").unlink()
                    (final_store / "metadata.json").unlink()
                    final_store.rmdir()
                    raise
            finally:
                temporary_binary.unlink(missing_ok=True)
        return {**meta, "path": str(destination), "reload_required": True}

    def _verified(self, pack_id):
        if not isinstance(pack_id, str) or not re.fullmatch(r"[a-f0-9]{32}", pack_id):
            raise DictionaryError("无效词包 ID。")
        self._safe_dirs()
        directory = self.store / pack_id
        binary = self.target / f"transime-{pack_id}.dict"
        metadata, source = directory / "metadata.json", directory / "source.tsv"
        if any(p.is_symlink() for p in (directory, metadata, source, binary)):
            raise DictionaryError("词包文件被替换为符号链接；拒绝操作。")
        try:
            meta = json.loads(metadata.read_text(encoding="utf-8"))
            if meta.get("schema") != 1 or meta.get("id") != pack_id or _sha(binary) != meta.get("sha256") or _sha(source) != meta.get("source_sha256"):
                raise DictionaryError("词包校验失败；拒绝删除或导出已改变的文件。")
        except (OSError, ValueError, AttributeError) as exc:
            raise DictionaryError(f"词包不可用或校验失败：{pack_id}") from exc
        return meta, directory, binary

    def list_packs(self):
        self._safe_dirs()
        if not self.store.exists():
            return []
        result = []
        for directory in sorted(self.store.iterdir()):
            if not re.fullmatch(r"[a-f0-9]{32}", directory.name):
                continue
            try:
                meta, _, binary = self._verified(directory.name)
                result.append({**meta, "path": str(binary), "status": "ready"})
            except DictionaryError as exc:
                result.append({"id": directory.name, "status": "error", "error": str(exc)})
        return result

    def remove_pack(self, pack_id):
        meta, directory, binary = self._verified(pack_id)
        if {p.name for p in directory.iterdir()} != {"metadata.json", "source.tsv"}:
            raise DictionaryError("词包目录含未知文件；拒绝删除。")
        binary.unlink()
        (directory / "source.tsv").unlink()
        (directory / "metadata.json").unlink()
        directory.rmdir()
        return {"id": meta["id"], "removed": True, "reload_required": True}

    def export_pack(self, pack_id, path):
        meta, directory, _ = self._verified(pack_id)
        path = Path(path)
        if path.suffix.lower() != ".tsv":
            raise DictionaryError("导出文件请使用 .tsv 扩展名。")
        # Exclusive creation prevents accidental replacement of any existing file.
        try:
            with path.open("xb") as stream:
                stream.write((directory / "source.tsv").read_bytes())
        except OSError as exc:
            raise DictionaryError(f"无法导出（不会覆盖已有文件）：{exc}") from exc
        return {"id": meta["id"], "path": str(path), "count": meta["count"]}
