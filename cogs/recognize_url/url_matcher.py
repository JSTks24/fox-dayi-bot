import json
import os
import re
import tempfile
from threading import Lock
from urllib.parse import urlparse


class URLMatcher:
    """URL 匹配引擎：标准化、匹配、提示词构建、LLM 响应解析。"""

    def __init__(
        self,
        good_path: str,
        bad_path: str,
        prompt_path: str,
    ):
        self._good_path = good_path
        self._bad_path = bad_path
        self._prompt_path = prompt_path
        self._json_write_lock = Lock()

    # ------------------------------------------------------------------
    # JSON 读写
    # ------------------------------------------------------------------

    def load_json(self, file_path: str) -> dict:
        try:
            with open(file_path, encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            print(f"⚠️ 文件不存在: {file_path}")
            return {}
        except json.JSONDecodeError as e:
            print(f"❌ JSON解析失败 {file_path}: {e}")
            return {}
        except Exception as e:
            print(f"❌ 加载JSON文件失败 {file_path}: {e}")
            return {}

    def save_json(self, file_path: str, data: dict) -> bool:
        temp_path = None
        try:
            directory = os.path.dirname(file_path) or '.'
            os.makedirs(directory, exist_ok=True)

            with self._json_write_lock:
                with tempfile.NamedTemporaryFile(
                    'w',
                    encoding='utf-8',
                    dir=directory,
                    delete=False,
                    suffix='.tmp'
                ) as f:
                    json.dump(data, f, ensure_ascii=False, indent=4)
                    temp_path = f.name

                os.replace(temp_path, file_path)
            return True
        except Exception as e:
            print(f"❌ 保存JSON文件失败 {file_path}: {e}")
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            return False

    # ------------------------------------------------------------------
    # URL 标准化 & 匹配
    # ------------------------------------------------------------------

    def normalize(self, url: str) -> tuple[str, str]:
        """标准化URL格式，返回 (domain, path)。"""
        raw_url = (url or '').strip()
        if not raw_url:
            return ('', '')

        parsed = urlparse(raw_url if '://' in raw_url else f'//{raw_url}')
        hostname = (parsed.hostname or '').lower()

        if not hostname:
            fallback = parsed.path.strip().rstrip('/')
            return (fallback.lower(), '')

        domain = hostname
        try:
            port = parsed.port
        except ValueError:
            port = None
        default_ports = {'http': 80, 'https': 443}
        if port and port != default_ports.get(parsed.scheme):
            domain += f':{port}'

        path = (parsed.path or '').rstrip('/')
        if path == '/':
            path = ''

        return (domain, path)

    def match(self, url: str) -> tuple[str, dict | None]:
        """分层匹配URL，返回 (status, entry_data_or_None)。

        status: "good" / "bad" / "unknown"
        """
        good_data = self.load_json(self._good_path)
        bad_data = self.load_json(self._bad_path)

        good_list = good_data.get('good', {})
        bad_list = bad_data.get('bad', {})

        domain, path = self.normalize(url)
        if not domain:
            return ('unknown', None)

        full_key = domain + path if path else domain

        def _make_entry(info: list, key: str) -> dict:
            return {
                'name': info[0] if len(info) > 0 else '',
                'description': info[1] if len(info) > 1 else '',
                'matched_key': key,
            }

        # --- 第 1 层：精确匹配 (domain + path) ---
        if full_key in bad_list:
            return ('bad', _make_entry(bad_list[full_key], full_key))
        if full_key in good_list:
            return ('good', _make_entry(good_list[full_key], full_key))

        # --- 第 2 层：纯域名匹配 (仅 domain，忽略 path) ---
        if path:
            if domain in bad_list:
                return ('bad', _make_entry(bad_list[domain], domain))
            if domain in good_list:
                return ('good', _make_entry(good_list[domain], domain))

        # --- 第 3 层：后缀匹配 (子域名场景) ---
        for key, info in bad_list.items():
            key_domain = key.split('/')[0]
            if domain.endswith('.' + key_domain):
                return ('bad', _make_entry(info, key))
        for key, info in good_list.items():
            key_domain = key.split('/')[0]
            if domain.endswith('.' + key_domain):
                return ('good', _make_entry(info, key))

        return ('unknown', None)

    # ------------------------------------------------------------------
    # 提示词 & LLM 解析
    # ------------------------------------------------------------------

    def build_prompt(self) -> str:
        try:
            with open(self._prompt_path, encoding='utf-8') as f:
                return f.read().strip()
        except Exception as e:
            print(f"❌ 构建提示词失败: {e}")
            return "Extract all URLs, IP addresses, and domain names visible in this screenshot. Output JSON: {\"urls\": [\"url1\", \"url2\"]}"

    def parse_llm_urls(self, llm_response: str) -> list[str]:
        """从 LLM 响应中解析 URL 列表。"""
        text = llm_response.strip()

        json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if json_match:
            text = json_match.group(1)

        try:
            data = json.loads(text)
            if isinstance(data, dict) and 'urls' in data:
                return [u for u in data['urls'] if isinstance(u, str) and u.strip()]
        except (json.JSONDecodeError, TypeError):
            pass

        urls = re.findall(
            r'(?:https?://)?(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}(?::\d+)?(?:/[^\s\'")\]},]*)?'
            r'|\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d+)?(?:/[^\s\'")\]},]*)?',
            llm_response,
        )
        return urls
