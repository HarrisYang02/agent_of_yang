#!/usr/bin/env python3
"""
parse_article.py — 通用文章解析器，支持学术论文和普通文章

用法:
    python parse_article.py <arxiv-id/url/doi/local-path> [--out output.txt]

支持的输入类型:
    - arXiv ID: 2404.1234 或 https://arxiv.org/abs/2404.1234
    - DOI: 10.xxxx/xxx 或 https://doi.org/10.xxxx/xxx
    - 普通 URL: http(s)://... (自动提取正文)
    - 本地文件: .pdf, .txt, .md, .html

输出格式:
    ---META---
    {"title": "...", "author": "...", "source": "...", "type": "academic/article", ...}
    ---TEXT---
    <纯文本内容>
"""

import sys
import os
import re
import json
import argparse
import urllib.request
import urllib.parse
import tempfile
from datetime import datetime

# ============================================================================
# arXiv 相关函数（复用自 parse_paper.py）
# ============================================================================

def is_arxiv_id(s):
    """判断是否为 arXiv ID"""
    return bool(re.match(r'^\d{4}\.\d{4,5}(v\d+)?$', s.strip()))

def extract_arxiv_id(s):
    """从字符串中提取 arXiv ID"""
    m = re.search(r'(\d{4}\.\d{4,5}(?:v\d+)?)', s)
    return m.group(1) if m else None

def fetch_arxiv_metadata(arxiv_id):
    """从 arXiv API 获取元数据"""
    clean_id = re.sub(r'v\d+$', '', arxiv_id)
    url = f"https://export.arxiv.org/api/query?id_list={clean_id}&max_results=1"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            xml = resp.read().decode('utf-8')
    except Exception as e:
        return {"arxiv_id": arxiv_id, "error": str(e)}

    import xml.etree.ElementTree as ET
    ns = {
        'atom': 'http://www.w3.org/2005/Atom',
        'arxiv': 'http://arxiv.org/schemas/atom',
    }
    try:
        root = ET.fromstring(xml)
        entry = root.find('atom:entry', ns)
        if entry is None:
            return {"arxiv_id": arxiv_id}

        title = (entry.findtext('atom:title', '', ns) or '').strip().replace('\n', ' ')
        abstract = (entry.findtext('atom:summary', '', ns) or '').strip().replace('\n', ' ')
        authors = [a.findtext('atom:name', '', ns).strip()
                   for a in entry.findall('atom:author', ns)]
        published = entry.findtext('atom:published', '', ns)
        year = published[:4] if published else ''

        return {
            "title": title,
            "author": authors[0] if authors else "",
            "authors": authors,
            "year": year,
            "publish_date": published[:10] if published else "",
            "abstract": abstract,
            "source": "arxiv",
            "arxiv_id": arxiv_id,
            "url": f"https://arxiv.org/abs/{clean_id}",
        }
    except Exception as e:
        return {"arxiv_id": arxiv_id, "error": str(e)}

def fetch_arxiv_html(arxiv_id):
    """尝试从 arXiv HTML 版本获取全文"""
    clean_id = re.sub(r'v\d+$', '', arxiv_id)
    url = f"https://arxiv.org/html/{clean_id}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode('utf-8', errors='replace')
        if 'No HTML' in html or 'not available' in html[:500]:
            return None
        return html_to_text(html)
    except Exception:
        return None

# ============================================================================
# DOI 处理
# ============================================================================

def is_doi(s):
    """判断是否为 DOI"""
    return bool(re.match(r'^10\.\d{4,}/\S+$', s.strip()) or 'doi.org' in s.lower())

def extract_doi(s):
    """从字符串中提取 DOI"""
    # 匹配标准 DOI 格式
    m = re.search(r'10\.\d{4,}/[^\s"<>]+', s)
    return m.group(0) if m else None

def fetch_doi_metadata(doi):
    """从 CrossRef API 获取 DOI 元数据"""
    url = f"https://api.crossref.org/works/{doi}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))

        msg = data.get('message', {})
        title = msg.get('title', [''])[0]
        authors = msg.get('author', [])
        author = ''
        if authors:
            a = authors[0]
            author = f"{a.get('given', '')} {a.get('family', '')}".strip()

        published = msg.get('published-print', msg.get('published-online', {}))
        year = str(published.get('date-parts', [['']])[0][0])

        return {
            "title": title,
            "author": author,
            "authors": [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in authors],
            "year": year,
            "publish_date": f"{year}-{published.get('date-parts', [[0,1]])[0][1]:02d}-{published.get('date-parts', [[0,1,1]])[0][2]:02d}" if len(published.get('date-parts', [[]])[0]) >= 2 else year,
            "abstract": msg.get('abstract', ''),
            "source": "doi",
            "doi": doi,
            "url": f"https://doi.org/{doi}",
        }
    except Exception as e:
        return {"doi": doi, "error": str(e)}

def fetch_doi_fulltext(doi):
    """尝试从 Unpaywall 获取开放访问的全文 PDF"""
    # Unpaywall API
    email = "user@example.com"  # 可配置
    url = f"https://api.unpaywall.org/v2/{doi}?email={email}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))

        pdf_url = data.get('best_oa_location', {}).get('url_for_pdf')
        if pdf_url:
            return download_and_parse_pdf(pdf_url)
    except Exception:
        pass
    return None

# ============================================================================
# HTML/网页处理
# ============================================================================

def html_to_text(html):
    """将 HTML 转换为纯文本"""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, 'html.parser')

        # 移除不需要的标签
        for tag in soup(['script', 'style', 'nav', 'header', 'footer',
                         'aside', 'iframe', 'noscript']):
            tag.decompose()

        # 提取文本
        text = soup.get_text(separator='\n', strip=True)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text
    except ImportError:
        # Fallback: 简单正则
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'&\w+;', ' ', text)
        text = re.sub(r'\s{2,}', ' ', text)
        return text

def extract_article_content(url):
    """
    提取网页文章内容（使用 readability 或 newspaper）
    优先尝试 newspaper3k，失败则用 BeautifulSoup
    """
    # 微信公众号链接自动添加参数
    if 'mp.weixin.qq.com' in url and 'type=' not in url:
        url = url + ('&' if '?' in url else '?') + 'type=appmsg'
        print(f"检测到微信链接，自动添加参数: {url}", file=sys.stderr)

    # 更完整的请求头，模拟真实浏览器
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Cache-Control': 'max-age=0',
        'Sec-Ch-Ua': '"Google Chrome";v="145", "Chromium";v="145", "Not-A.Brand";v="99"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
    }

    try:
        # 尝试使用 newspaper3k
        from newspaper import Article
        article = Article(url)
        article.download()
        article.parse()

        meta = {
            "title": article.title or "",
            "author": ", ".join(article.authors) if article.authors else "",
            "publish_date": article.publish_date.strftime('%Y-%m-%d') if article.publish_date else "",
            "source": urllib.parse.urlparse(url).netloc,
            "url": url,
        }
        return meta, article.text
    except ImportError:
        pass
    except Exception as e:
        print(f"Warning: newspaper3k failed: {e}", file=sys.stderr)

    # Fallback: 直接下载并用 BeautifulSoup 解析
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            # 处理可能的 gzip 压缩
            content = resp.read()
            try:
                import gzip
                html = gzip.decompress(content).decode('utf-8', errors='replace')
            except:
                html = content.decode('utf-8', errors='replace')

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, 'html.parser')

        # 提取标题
        title = soup.find('h1') or soup.find('title')
        title = title.get_text().strip() if title else ""

        # 提取作者（常见 meta 标签）
        author_meta = soup.find('meta', attrs={'name': re.compile(r'author', re.I)})
        author = author_meta.get('content', '') if author_meta else ""

        # 知乎特定处理
        if 'zhihu.com' in url:
            # 知乎文章用 RichContent 类
            content = soup.find('div', class_=re.compile(r'RichContent|Post-RichText|content'))
        # 微信公众号特定处理
        elif 'weixin.qq.com' in url:
            content = soup.find('div', id='js_content')
        # 通用处理
        else:
            content = soup.find('article') or soup.find('main') or soup.find('div', class_=re.compile(r'content|article|post', re.I))

        if content:
            for tag in content(['script', 'style', 'nav', 'header', 'footer', 'aside']):
                tag.decompose()
            text = content.get_text(separator='\n', strip=True)
        else:
            text = html_to_text(html)

        meta = {
            "title": title,
            "author": author,
            "publish_date": datetime.now().strftime('%Y-%m-%d'),
            "source": urllib.parse.urlparse(url).netloc,
            "url": url,
        }
        return meta, text
    except Exception as e:
        raise RuntimeError(f"网页提取失败: {e}")

# ============================================================================
# PDF 处理
# ============================================================================

def pdf_to_text(pdf_path):
    """使用 PyMuPDF 提取 PDF 文本"""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(pdf_path)
        parts = []
        for page in doc:
            parts.append(page.get_text("text"))
        text = '\n'.join(parts)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text
    except ImportError:
        raise RuntimeError("PyMuPDF 未安装。运行: pip install pymupdf")
    except Exception as e:
        raise RuntimeError(f"PDF 提取失败: {e}")

def download_and_parse_pdf(url):
    """下载并解析 PDF"""
    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
        tmp_path = tmp.name
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            with open(tmp_path, 'wb') as f:
                f.write(resp.read())
        return pdf_to_text(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

# ============================================================================
# 文档类型判断
# ============================================================================

def detect_document_type(text, meta):
    """
    自动判断文档类型：academic（学术论文）或 article（普通文章）

    判断依据：
    - 学术论文特征：Abstract, Introduction, Method, Results, References, 大量引用
    - 普通文章特征：缺少学术结构，口语化
    """
    text_lower = text.lower()

    # 学术论文关键章节
    academic_sections = [
        'abstract', 'introduction', 'method', 'experiment',
        'result', 'conclusion', 'reference'
    ]
    section_count = sum(1 for s in academic_sections if s in text_lower)

    # 引用数量（粗略统计 [数字] 格式）
    citation_count = len(re.findall(r'\[\d+\]', text))

    # 数学公式（简单判断）
    has_math = bool(re.search(r'\\[a-z]+\{|_{[a-z]}|\^{[a-z]}', text))

    # arXiv/DOI 来源直接判定为学术论文
    if meta.get('source') in ['arxiv', 'doi']:
        return 'academic'

    # 综合判断
    if section_count >= 4 and citation_count > 20:
        return 'academic'
    elif section_count >= 3 and (citation_count > 10 or has_math):
        return 'academic'
    else:
        return 'article'

# ============================================================================
# 主处理流程
# ============================================================================

def process_arxiv(arxiv_id):
    """处理 arXiv 论文"""
    meta = fetch_arxiv_metadata(arxiv_id)

    # 优先 HTML
    text = fetch_arxiv_html(arxiv_id)
    if text:
        meta['_source'] = 'arxiv-html'
        return meta, text

    # 备用 PDF
    clean_id = re.sub(r'v\d+$', '', arxiv_id)
    pdf_url = f"https://arxiv.org/pdf/{clean_id}.pdf"
    text = download_and_parse_pdf(pdf_url)
    meta['_source'] = 'arxiv-pdf'
    return meta, text

def process_doi(doi):
    """处理 DOI 文献"""
    meta = fetch_doi_metadata(doi)

    # 尝试获取全文
    text = fetch_doi_fulltext(doi)
    if text:
        meta['_source'] = 'doi-pdf'
        return meta, text

    # 如果无法获取全文，返回摘要
    text = meta.get('abstract', '无法获取全文。仅有摘要。')
    meta['_source'] = 'doi-abstract-only'
    return meta, text

def process_local_pdf(path):
    """处理本地 PDF"""
    text = pdf_to_text(path)
    meta = {
        "title": os.path.basename(path),
        "author": "",
        "year": "",
        "publish_date": datetime.now().strftime('%Y-%m-%d'),
        "abstract": "",
        "source": "local-pdf",
        "url": path,
        "_source": "local-pdf",
    }
    return meta, text

def process_local_text(path):
    """处理本地文本文件 (.txt, .md)"""
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()

    meta = {
        "title": os.path.basename(path),
        "author": "",
        "year": "",
        "publish_date": datetime.now().strftime('%Y-%m-%d'),
        "abstract": "",
        "source": "local-text",
        "url": path,
        "_source": "local-text",
    }
    return meta, text

def process_url(url):
    """处理普通 URL"""
    # 判断是否为 PDF
    if url.lower().endswith('.pdf') or '/pdf/' in url.lower():
        text = download_and_parse_pdf(url)
        meta = {
            "title": url.split('/')[-1],
            "author": "",
            "year": "",
            "publish_date": datetime.now().strftime('%Y-%m-%d'),
            "abstract": "",
            "source": "url-pdf",
            "url": url,
            "_source": "url-pdf",
        }
        return meta, text

    # 否则作为网页文章处理
    meta, text = extract_article_content(url)
    meta['_source'] = 'url-html'
    return meta, text

# ============================================================================
# 主函数
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description='通用文章解析器')
    parser.add_argument('input', help='arXiv ID, DOI, URL, 或本地文件路径')
    parser.add_argument('--out', default='-', help='输出文件（默认: stdout）')
    parser.add_argument('--meta-only', action='store_true', help='仅输出元数据')
    args = parser.parse_args()

    inp = args.input.strip()

    # 路由到对应处理器
    try:
        # 检测 arXiv
        arxiv_id = extract_arxiv_id(inp) if ('arxiv.org' in inp or is_arxiv_id(inp)) else None
        if arxiv_id:
            meta, text = process_arxiv(arxiv_id)

        # 检测 DOI
        elif is_doi(inp):
            doi = extract_doi(inp) or inp
            meta, text = process_doi(doi)

        # 本地 PDF
        elif os.path.isfile(inp) and inp.lower().endswith('.pdf'):
            meta, text = process_local_pdf(inp)

        # 本地文本文件
        elif os.path.isfile(inp) and (inp.lower().endswith('.txt') or inp.lower().endswith('.md')):
            meta, text = process_local_text(inp)

        # URL
        elif inp.startswith('http://') or inp.startswith('https://'):
            meta, text = process_url(inp)

        else:
            print(f"错误: 无法识别输入类型: {inp}", file=sys.stderr)
            sys.exit(1)

        # 自动判断文档类型
        doc_type = detect_document_type(text, meta)
        meta['type'] = doc_type

        # 构建输出
        out_lines = []
        out_lines.append('---META---')
        out_lines.append(json.dumps(meta, ensure_ascii=False, indent=2))
        if not args.meta_only:
            out_lines.append('---TEXT---')
            out_lines.append(text)

        output = '\n'.join(out_lines)

        # 输出
        if args.out == '-':
            # Windows 控制台编码问题处理
            try:
                sys.stdout.write(output)
            except UnicodeEncodeError:
                # 如果控制台不支持 UTF-8，替换无法显示的字符
                sys.stdout.buffer.write(output.encode('utf-8'))
        else:
            with open(args.out, 'w', encoding='utf-8') as f:
                f.write(output)
            print(f"已写入: {args.out}", file=sys.stderr)

    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
