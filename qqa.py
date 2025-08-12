import pandas as pd
import streamlit as st
from io import BytesIO
import base64
import os
import sys
from datetime import datetime
import json
import time
import hashlib
import asyncio
import aiohttp
from aiohttp import ClientTimeout
import socket
import logging
import re
from dotenv import load_dotenv
import requests
from st_aggrid import GridOptionsBuilder, AgGrid, GridUpdateMode, DataReturnMode
import subprocess
try:
    import aiohttp
    print("aiohttp version:", aiohttp.__version__)
except ImportError:
    print("ERROR: aiohttp not installed!")# === 初始化日志和配置 ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# === 依赖检查 ===
REQUIRED_PACKAGES = [
    'pandas',
    'numpy',
    'openpyxl',
    'xlsxwriter',
    'xlrd',
    'st_aggrid',  # 实际导入包名
    'aiohttp',
    'dotenv'  # 实际导入包名
]


def check_dependencies():
    missing = []
    for package in REQUIRED_PACKAGES:
        try:
            __import__(package)
        except ImportError:
            missing.append(package)

    # 安装时使用的PyPI名称映射
    pypi_names = {
        'st_aggrid': 'streamlit-aggrid',
        'dotenv': 'python-dotenv'
    }

    if missing:
        # 转换为PyPI包名
        install_packages = [pypi_names.get(pkg, pkg) for pkg in missing]
        st.warning(f"正在安装缺少的依赖: {', '.join(install_packages)}")
        try:
            subprocess.check_call([
                sys.executable,
                "-m",
                "pip",
                "install",
                *install_packages
            ])
            st.experimental_rerun()
        except Exception as e:
            st.error(f"依赖安装失败: {str(e)}")
            st.stop()


# === 安全初始化会话状态 ===
def initialize_session_state():
    """初始化所有会话状态键值，防止KeyError"""
    defaults = {
        'df': pd.DataFrame(
            columns=['记录', '物业', '地址', '房号', '联系方式', '清洗内容', '数量', '金额', '付款方式', '备注']),
        'input_text': "",
        'last_processed': "",
        'auto_save_counter': 0,
        'api_endpoint': "https://api.deepseek.com/v1/chat/completions",
        'auto_process': False,
        'cache_dict': {},
        'batch_size': 5,
        'active_endpoints': [],
        'model_version': "deepseek-chat",
        'api_key': "",
        'manual_api_key': "",
        'api_call_count': 0,
        'api_response_time': 0,
        'cached_df': pd.DataFrame(
            columns=['记录', '物业', '地址', '房号', '联系方式', '清洗内容', '数量', '金额', '付款方式', '备注']),
        'debug_mode': False,
        'api_key_saved': False,
        'processed_records': set(),
        'max_records': 50  # 增加最大记录数到50
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


# === 必须作为第一个Streamlit命令 ===
st.set_page_config(
    page_title="🧹 清洗服务记录转换工具",
    page_icon="🧹",
    layout="wide",
    initial_sidebar_state="expanded"
)


# === 安全获取DeepSeek API密钥 ===
def get_deepseek_api_key():
    """安全获取API密钥的多层策略"""
    api_key = ""
    key_sources = []

    # 1. 尝试从环境变量获取
    if 'DEEPSEEK_API_KEY' in os.environ:
        api_key = os.environ['DEEPSEEK_API_KEY']
        key_sources.append("环境变量")

    # 2. 尝试从st.secrets获取
    try:
        if not api_key and 'DEEPSEEK_API_KEY' in st.secrets:
            api_key = st.secrets['DEEPSEEK_API_KEY']
            key_sources.append("Streamlit Secrets")
    except Exception:
        pass

    # 3. 尝试从.env文件加载
    if not api_key and os.path.exists('.env'):
        try:
            load_dotenv()
            api_key = os.getenv('DEEPSEEK_API_KEY')
            if api_key:
                key_sources.append(".env文件")
        except Exception:
            pass

    # 4. 使用手动输入的密钥
    if st.session_state.manual_api_key:
        api_key = st.session_state.manual_api_key
        key_sources.append("手动输入")
        st.session_state.api_key_saved = True

    # 5. 验证密钥格式
    if api_key:
        # 清理空格
        if " " in api_key:
            api_key = api_key.replace(" ", "")
            logger.info("已清理API密钥中的空格")

        # 格式验证 - 修正为35字符
        if not api_key.startswith("sk-"):
            st.error("⚠️ API密钥必须以'sk-'开头")
            logger.error(f"无效的API密钥开头: {api_key[:10]}...")
            api_key = ""
        elif len(api_key) < 35:
            st.error(f"⚠️ API密钥长度不足：当前长度{len(api_key)}，要求≥35字符")
            logger.error(f"密钥长度不足: {len(api_key)}字符")
            api_key = ""
        else:
            # 保存验证通过的密钥
            st.session_state.api_key = api_key
            logger.info(f"API密钥验证通过，长度: {len(api_key)}字符")

    return api_key, key_sources


# === 缓存机制 ===
def generate_cache_key(prompt: str) -> str:
    """生成缓存键避免重复请求"""
    # 确保输入是字符串
    if not isinstance(prompt, str):
        prompt = str(prompt)
    clean_prompt = re.sub(r'\s+', '', prompt)
    return hashlib.md5(clean_prompt.encode('utf-8')).hexdigest()


# === DeepSeek API调用 ===
async def async_deepseek_request(session, messages, model=None, temperature=0.3):
    """异步API请求核心函数"""
    if not model:
        model = st.session_state.get("model_version", "deepseek-chat")

    # 安全获取API密钥
    api_key = st.session_state.get("api_key", "")
    if not api_key:
        logger.error("API密钥缺失")
        st.error("API密钥缺失，请检查配置")
        return None

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 2048,
        "stream": False
    }

    # 缓存检查 - 使用紧凑JSON格式
    cache_key = generate_cache_key(json.dumps(messages, separators=(',', ':')))
    if cache_key in st.session_state.get("cache_dict", {}):
        logger.info(f"使用缓存响应: {cache_key[:8]}...")
        return st.session_state.cache_dict[cache_key]

    try:
        start_time = time.time()
        timeout = ClientTimeout(total=15)  # 减少超时时间到15秒
        async with session.post(
                st.session_state.get("api_endpoint", "https://api.deepseek.com/v1/chat/completions"),
                json=payload,
                headers=headers,
                timeout=timeout
        ) as response:
            if response.status == 200:
                response_data = await response.json()
                content = response_data['choices'][0]['message']['content']

                # 更新性能统计
                elapsed = time.time() - start_time
                st.session_state.api_call_count += 1
                if st.session_state.api_call_count > 1:
                    total_time = st.session_state.api_response_time * (st.session_state.api_call_count - 1)
                    st.session_state.api_response_time = (total_time + elapsed) / st.session_state.api_call_count
                else:
                    st.session_state.api_response_time = elapsed

                # 存入缓存
                st.session_state.cache_dict[cache_key] = content
                return content
            else:
                error_text = await response.text()
                logger.error(f"API错误: {response.status} - {error_text}")

                # 在界面上显示详细错误信息
                error_msg = f"API错误 (HTTP {response.status}): "
                if response.status == 401:
                    error_msg += "未授权 - 请检查API密钥是否正确"
                elif response.status == 403:
                    error_msg += "禁止访问 - 请检查API权限"
                elif response.status == 429:
                    error_msg += "请求过多 - 请稍后再试"
                else:
                    error_msg += error_text[:500] + ("..." if len(error_text) > 500 else "")

                st.error(error_msg)
                return None

    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        error_msg = f"请求异常: {str(e)}"
        logger.error(error_msg)

        # 提供更友好的网络错误信息
        if "Cannot connect to host" in str(e):
            st.error("无法连接到API服务器，请检查网络连接或尝试切换API端点")
        elif "Timeout" in str(e):
            st.error("请求超时，请尝试减小批量大小或稍后再试")
        else:
            st.error(error_msg)

        return None


# === 检查端点连通性 ===
def is_endpoint_reachable(endpoint):
    try:
        host = endpoint.split("//")[-1].split("/")[0]
        socket.getaddrinfo(host, 443)
        return True
    except Exception:
        return False


# === 获取可用端点列表 ===
def get_available_endpoints():
    # 缓存检查
    if 'available_endpoints' in st.session_state and st.session_state.available_endpoints:
        return st.session_state.available_endpoints

    endpoints = {
        "官方主端点(v1)": "https://api.deepseek.com/v1/chat/completions",
        "官方主端点(旧版)": "https://api.deepseek.com/chat/completions",
        "备用端点": "https://api.deepseek.cc/chat/completions",
        "国内优化端点": "https://api.deepseek.com.cn/chat/completions"
    }

    available = []
    for name, url in endpoints.items():
        if is_endpoint_reachable(url):
            available.append((name, url))

    if not available:
        try:
            ip_list = socket.getaddrinfo("api.deepseek.com", 443)
            if ip_list:
                ip = ip_list[0][4][0]
                available.append(("直接IP连接", f"https://{ip}/chat/completions"))
        except Exception:
            pass

    # 缓存结果
    st.session_state.available_endpoints = available
    return available


# === 测试API连接 ===
def test_api_connection():
    if not st.session_state.get("api_key", ""):
        st.error("请先输入并保存API密钥")
        return

    # 显示当前使用的密钥信息
    masked_key = f"{st.session_state.api_key[:6]}...{st.session_state.api_key[-4:]}"
    st.info(f"测试使用的密钥: {masked_key} (长度: {len(st.session_state.api_key)}字符)")

    # 准备测试消息
    test_messages = [
        {"role": "system", "content": "你是一个测试助手，只需回复'连接成功'"},
        {"role": "user", "content": "测试API连接"}
    ]

    # 准备API请求
    payload = {
        "model": st.session_state.model_version,
        "messages": test_messages,
        "temperature": 0.1,
        "max_tokens": 10
    }

    headers = {
        "Authorization": f"Bearer {st.session_state.api_key}",
        "Content-Type": "application/json"
    }

    try:
        with st.spinner("测试API连接中..."):
            response = requests.post(
                st.session_state.api_endpoint,
                headers=headers,
                json=payload,
                timeout=15
            )

            if response.status_code == 200:
                st.success("🎉 API连接成功！")
                response_data = response.json()
                st.json(response_data)
            else:
                error_msg = f"❌ 连接失败 (HTTP {response.status_code}): "
                if response.status_code == 401:
                    error_msg += "未授权 - 请检查API密钥是否正确"
                elif response.status_code == 403:
                    error_msg += "禁止访问 - 请检查API权限"
                elif response.status_code == 429:
                    error_msg += "请求过多 - 请稍后再试"
                else:
                    error_msg += response.text[:500] + ("..." if len(response.text) > 500 else "")

                st.error(error_msg)
    except Exception as e:
        error_msg = f"⚠️ 连接异常: {str(e)}"
        st.error(error_msg)

        # 提供诊断建议
        st.warning("""
        **连接失败可能原因:**
        1. API密钥无效或已过期
        2. 网络连接问题 (尝试切换网络)
        3. 防火墙阻止了API访问
        4. DeepSeek服务暂时不可用
        5. 端点地址不正确

        **排查步骤:**
        - 检查密钥格式是否正确 (应以'sk-'开头，长度≥35字符)
        - 尝试在侧边栏切换API端点
        - 确认您的账户有可用配额
        - 检查防火墙设置
        """)


# === 处理批次 ===
async def process_batch(session, messages, endpoint, batch_text):
    """处理单批次记录"""
    try:
        response = await async_deepseek_request(session, messages)
        if response:
            # 在调试模式下显示原始响应
            if st.session_state.debug_mode:
                st.sidebar.subheader("API原始响应")
                st.sidebar.code(response[:1000] + "..." if len(response) > 1000 else response, language='json')
            return response
        return None
    except Exception as e:
        logger.error(f"处理批次时出错: {str(e)}")
        st.error(f"处理批次时出错: {str(e)}")
        return None


# === 记录哈希生成 ===
def generate_record_hash(record):
    """为记录生成唯一哈希值，用于去重"""
    # 使用关键字段生成哈希
    key_fields = [
        record.get('房号', ''),
        record.get('联系方式', ''),
        record.get('清洗内容', ''),
        record.get('金额', '')
    ]
    key_string = "|".join(str(field) for field in key_fields)
    return hashlib.md5(key_string.encode('utf-8')).hexdigest()


# === 处理记录 ===
async def process_records(input_text):
    """处理输入文本并转换为结构化数据"""
    # 保存当前文本
    st.session_state.input_text = input_text

    if not st.session_state.api_key:
        st.error("缺少DeepSeek API密钥！请按照侧边栏说明配置")
        return

    # 显示当前使用的密钥信息
    masked_key = f"{st.session_state.api_key[:6]}...{st.session_state.api_key[-4:]}"
    st.info(f"使用的API密钥: {masked_key} (长度: {len(st.session_state.api_key)}字符)")

    # 获取可用端点
    available_endpoints = get_available_endpoints()
    if not available_endpoints:
        st.error("无法连接到任何DeepSeek API端点，请检查网络连接！")
        return

    # 创建进度条
    progress_bar = st.progress(0)
    status_container = st.empty()
    status_text = f"使用端点: {available_endpoints[0][0]} ({available_endpoints[0][1]})"
    status_container.text(status_text)

    # 系统提示 - 简化提示以提高响应速度
    system_prompt = """
    你是一个文本解析专家，负责将清洗服务记录文本转换为结构化的JSON数据。请输出清晰的JSON格式。

    ### 输出格式:
    [
        {
            "物业": "物业名称",
            "地址": "地址",
            "房号": "房号",
            "联系方式": "联系方式",
            "清洗内容": "清洗内容",
            "数量": "数量",
            "金额": "金额",
            "付款方式": "付款方式",
            "备注": "备注"
        }
    ]

    ### 示例:
    输入: 融创 凡尔赛领馆四期 16栋27-7 15223355185 空调内外机清洗 1 380 未支付 有异味
    输出: [{"物业":"融创","地址":"凡尔赛领馆四期","房号":"16栋27-7","联系方式":"15223355185","清洗内容":"空调内外机清洗","数量":"1","金额":"380","付款方式":"未支付","备注":"有异味"}]
    """

    # 限制最大记录数
    max_records = st.session_state.max_records
    lines = [line.strip() for line in input_text.strip().split('\n') if line.strip()]
    line_count = len(lines)

    if line_count > max_records:
        st.warning(f"一次最多处理{max_records}条记录（当前{line_count}条），请分批处理")
        return

    # 分批处理
    batch_size = st.session_state.batch_size
    num_batches = (line_count + batch_size - 1) // batch_size
    all_data = []
    errors = []
    new_records = []  # 存储新添加的记录

    # 创建异步会话
    async with aiohttp.ClientSession() as session:
        tasks = []
        batch_contents = []
        batch_lines_list = []  # 存储每个批次的原始行列表
        for i in range(num_batches):
            start_idx = i * batch_size
            end_idx = min((i + 1) * batch_size, line_count)
            batch_lines = lines[start_idx:end_idx]

            if not batch_lines:
                continue

            batch_text = "\n".join(batch_lines)

            # 准备API请求 - 确保发送完整文本
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"请解析以下清洗服务记录文本并输出为JSON格式:\n{batch_text}"}
            ]

            # 为每个批次使用第一个可用端点
            endpoint = available_endpoints[0][1]
            tasks.append(process_batch(session, messages, endpoint, batch_text))
            batch_contents.append(batch_text)
            batch_lines_list.append(batch_lines)  # 保存原始行列表

        if not tasks:
            st.info("没有新的文本需要处理")
            return

        # 执行所有任务
        results = await asyncio.gather(*tasks)

        # 处理结果
        for i, (result, content) in enumerate(zip(results, batch_contents)):
            progress = int((i + 1) * 100 / len(tasks))
            progress_bar.progress(progress)

            # 更新状态信息
            status_text = f"""
            **处理进度**: {progress}%  
            **当前批次**: {i + 1}/{len(tasks)}  
            **API调用次数**: {st.session_state.api_call_count}  
            **平均响应时间**: {st.session_state.api_response_time:.2f}s  
            **已解析记录**: {len(all_data)}
            """
            status_container.markdown(status_text)

            if result is None:
                errors.append(f"批次 {i + 1} 处理失败")
                continue

            # 在调试模式下显示原始响应
            if st.session_state.debug_mode:
                st.sidebar.subheader(f"批次 {i + 1} 原始响应")
                st.sidebar.code(result[:1000] + "..." if len(result) > 1000 else result, language='json')

            try:
                # 关键修复：处理API返回的JSON字符串
                clean_result = result.strip()

                # 尝试提取JSON部分（可能被代码块包裹）
                json_match = re.search(r'```json\n([\s\S]*?)\n```', clean_result)
                if json_match:
                    clean_result = json_match.group(1)

                # 尝试解析JSON
                parsed_data = json.loads(clean_result)

                # 处理单个对象的情况（转换为列表）
                if isinstance(parsed_data, dict):
                    parsed_data = [parsed_data]

                if isinstance(parsed_data, list):
                    for record in parsed_data:
                        if isinstance(record, dict):
                            # 生成记录哈希值用于去重
                            record_hash = generate_record_hash(record)

                            # 检查记录是否已存在
                            if record_hash not in st.session_state.processed_records:
                                # 确保所有字段都有值
                                all_data.append([
                                    "",  # 记录列（空）
                                    record.get('物业', ''),  # 物业（由用户自行填写）
                                    record.get('地址', ''),
                                    record.get('房号', ''),
                                    record.get('联系方式', ''),
                                    record.get('清洗内容', ''),
                                    record.get('数量', '1'),  # 默认数量为1
                                    record.get('金额', ''),
                                    record.get('付款方式', '未支付'),
                                    record.get('备注', '')
                                ])
                                new_records.append(record)
                                st.session_state.processed_records.add(record_hash)
                            else:
                                logger.info(f"跳过重复记录: {record.get('房号', '')}-{record.get('联系方式', '')}")
                        else:
                            logger.warning(f"跳过非字典类型记录: {type(record)}")
                else:
                    errors.append(f"批次 {i + 1} 返回结果不是列表格式")
            except json.JSONDecodeError as e:
                errors.append(f"批次 {i + 1} JSON解析失败: {str(e)}")
                # 显示解析失败的原始内容
                st.error(f"JSON解析失败: {str(e)}")
                st.code(f"原始内容: {clean_result[:500]}{'...' if len(clean_result) > 500 else ''}", language='text')
                # 尝试直接处理文本作为最后手段 - 使用原始行列表
                st.warning("尝试直接处理文本（逐行）...")
                current_batch_lines = batch_lines_list[i]  # 获取当前批次的原始行
                for line in current_batch_lines:  # 逐行处理
                    parts = line.split(maxsplit=8)  # 最多分割9部分
                    if len(parts) >= 5:  # 最少需要5个字段
                        # 确保有足够的字段，不足的用空字符串填充
                        padded_parts = parts + [''] * (9 - len(parts))
                        all_data.append([
                            "",  # 记录列（空）
                            padded_parts[0],  # 物业
                            padded_parts[1] if len(parts) > 1 else '',  # 地址
                            padded_parts[2] if len(parts) > 2 else '',  # 房号
                            padded_parts[3] if len(parts) > 3 else '',  # 联系方式
                            padded_parts[4] if len(parts) > 4 else '',  # 清洗内容
                            padded_parts[5] if len(parts) > 5 else '1',  # 数量，默认1
                            padded_parts[6] if len(parts) > 6 else '',  # 金额
                            padded_parts[7] if len(parts) > 7 else '未支付',  # 付款方式，默认未支付
                            padded_parts[8] if len(parts) > 8 else ''  # 备注
                        ])
                    else:
                        errors.append(f"行 '{line}' 字段不足，已跳过")
            except Exception as e:
                errors.append(f"批次 {i + 1} 处理异常: {str(e)}")
                st.error(f"处理异常: {str(e)}")

    progress_bar.progress(100)
    time.sleep(0.5)
    progress_bar.empty()
    status_container.empty()

    if all_data:
        # 创建新解析出的DataFrame - 使用新的列名
        columns = ['记录', '物业', '地址', '房号', '联系方式', '清洗内容', '数量', '金额', '付款方式', '备注']
        new_df = pd.DataFrame(all_data, columns=columns)

        # 如果当前已有数据，则追加新数据
        if 'df' in st.session_state and not st.session_state.df.empty:
            # 保留原有的自行填写内容
            existing_df = st.session_state.df

            # 追加新数据
            combined_df = pd.concat([existing_df, new_df], ignore_index=True)
            st.session_state.df = combined_df
        else:
            # 首次处理，直接赋值
            st.session_state.df = new_df

        st.session_state.last_processed = input_text
        st.session_state.cached_df = st.session_state.df.copy()
        st.session_state.auto_save_counter += 1

        success_msg = f"成功添加 {len(new_records)} 条新记录！"
        if len(tasks) > 1:
            success_msg += f" (分{len(tasks)}批处理)"
        st.success(success_msg)

        # 显示新添加的记录
        with st.expander("📋 查看新添加的记录", expanded=False):
            st.dataframe(new_df)

        # 显示性能统计
        if st.session_state.api_call_count > 0:
            st.info(f"API调用次数: {st.session_state.api_call_count}次")
            st.info(f"平均响应时间: {st.session_state.api_response_time:.2f}秒")

            # 检查是否在10秒内完成
            total_time = st.session_state.api_response_time * st.session_state.api_call_count
            if total_time > 10:
                st.warning(f"处理时间较长: {total_time:.2f}秒，请尝试减小批量大小")
    else:
        if not errors:
            st.info("没有新记录需要添加")
        else:
            st.error("未能解析出任何记录，请检查输入格式或API响应！")
            st.warning(f"共发现 {len(errors)} 条错误")
            for error in errors:
                st.error(error)


# === 显示结果 ===
def display_results():
    """显示处理结果和导出选项"""
    st.subheader("清洗服务记录表格（可编辑）")

    # 添加手动保存按钮
    if st.button("💾 手动保存当前表格", key="save_table_button"):
        st.session_state.cached_df = st.session_state.df.copy()
        st.session_state.auto_save_counter += 1
        st.success("表格已保存！")

    # 添加清空表格按钮
    if st.button("🗑️ 清空表格", key="clear_table_button"):
        st.session_state.df = pd.DataFrame(
            columns=['记录', '物业', '地址', '房号', '联系方式', '清洗内容', '数量', '金额', '付款方式', '备注'])
        st.session_state.processed_records = set()
        st.success("表格已清空！")

    # 使用st_aggrid展示表格 - 增加默认列宽
    gb = GridOptionsBuilder.from_dataframe(st.session_state.df)

    # 设置各列宽度
    column_widths = {
        '记录': 100,
        '物业': 120,
        '地址': 200,
        '房号': 100,
        '联系方式': 120,
        '清洗内容': 250,
        '数量': 80,
        '金额': 100,
        '付款方式': 120,
        '备注': 300
    }

    for col in st.session_state.df.columns:
        width = column_widths.get(col, 150)  # 默认150px
        gb.configure_column(col, width=width, editable=True)

    gb.configure_grid_options(domLayout='normal', enableRangeSelection=True)
    grid_options = gb.build()

    grid_response = AgGrid(
        st.session_state.df,
        gridOptions=grid_options,
        data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
        update_mode=GridUpdateMode.MODEL_CHANGED,
        fit_columns_on_grid_load=False,  # 使用自定义宽度
        enable_enterprise_modules=False,
        allow_unsafe_jscode=True,
        use_container_width=True,
        height=500,
        theme='streamlit'
    )

    # 保存编辑后的 DataFrame
    st.session_state.df = grid_response['data']

    # 添加统计信息
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("总记录数", len(st.session_state.df))

    # 金额统计
    if '金额' in st.session_state.df.columns:
        try:
            # 尝试将金额转换为数值类型
            st.session_state.df['金额'] = pd.to_numeric(st.session_state.df['金額'], errors='coerce')
            total_amount = st.session_state.df['金额'].sum()
            col2.metric("总金额", f"¥{total_amount:.2f}")
        except:
            col2.metric("总金额", "数据格式错误")
    else:
        col2.metric("总金额", "无数据")

    # 付款方式统计
    if '付款方式' in st.session_state.df.columns:
        payment_counts = st.session_state.df['付款方式'].value_counts()
        col3.metric("未支付数量", payment_counts.get('未支付', 0))
        col4.metric("已支付数量", payment_counts.get('已支付', 0))
    else:
        col3.metric("未支付数量", "无数据")
        col4.metric("已支付数量", "无数据")

    # 导出Excel功能
    st.subheader("导出数据")
    output = BytesIO()

    try:
        # 使用xlsxwriter引擎
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            st.session_state.df.to_excel(writer, index=False, sheet_name='清洗服务记录')
            workbook = writer.book
            worksheet = writer.sheets['清洗服务记录']

            # 设置列宽
            for idx, col in enumerate(st.session_state.df.columns):
                max_len = max(st.session_state.df[col].astype(str).map(len).max(), len(col)) + 2
                worksheet.set_column(idx, idx, max_len)

            # 设置付款方式颜色
            format_red = workbook.add_format({'bg_color': '#FFC7CE'})
            format_green = workbook.add_format({'bg_color': '#C6EFCE'})

            # 付款方式在第8列 (I列)
            for row in range(1, len(st.session_state.df)):
                cell_value = st.session_state.df.iloc[row, 8]  # 付款方式列索引为8
                if cell_value == "未支付":
                    worksheet.write(row + 1, 8, cell_value, format_red)
                elif cell_value == "已支付":
                    worksheet.write(row + 1, 8, cell_value, format_green)

            # 冻结首行
            worksheet.freeze_panes(1, 0)

            # 自动筛选
            worksheet.autofilter(0, 0, len(st.session_state.df), len(st.session_state.df.columns) - 1)

    except Exception as e:
        st.error(f"Excel导出错误: {str(e)}")
        return

    # 生成下载链接
    excel_data = output.getvalue()
    b64 = base64.b64encode(excel_data).decode()
    href = f'<a href="data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,{b64}" download="清洗服务记录_{datetime.now().strftime("%Y%m%d_%H%M")}.xlsx">⬇️ 下载Excel文件</a>'
    st.markdown(href, unsafe_allow_html=True)


# === 异步处理函数 ===
async def async_process_records(input_text):
    """异步处理记录的包装函数"""
    await process_records(input_text)


# === 侧边栏配置 ===
def sidebar_config():
    # 确保会话状态初始化
    initialize_session_state()

    with st.sidebar:
        st.header("⚙️ 配置中心")

        # API密钥状态
        with st.expander("🔑 API密钥设置", expanded=True):
            # 显示当前密钥状态
            if st.session_state.api_key:
                masked_key = f"{st.session_state.api_key[:6]}...{st.session_state.api_key[-4:]}"
                key_length = len(st.session_state.api_key)
                st.success(f"**密钥状态**: ✔️ 已保存有效密钥\n\n**格式**: {masked_key}\n**长度**: {key_length}字符")
            else:
                st.warning("**密钥状态**: ❌ 未配置")

            # 密钥输入区域
            manual_key = st.text_input(
                "输入DeepSeek API密钥 (sk-开头)",
                type="password",
                value=st.session_state.manual_api_key,
                key="manual_api_key_input",
                help="从DeepSeek平台获取API密钥，格式为sk-xxxxxxxxxxxxxxxx"
            )

            # 保存密钥按钮
            if st.button("💾 保存密钥", key="save_api_key_button"):
                st.session_state.manual_api_key = manual_key
                # 触发密钥验证和保存
                api_key, _ = get_deepseek_api_key()
                if st.session_state.api_key:
                    st.success("API密钥保存成功！")
                    st.session_state.api_key_saved = True
                else:
                    st.error("密钥无效，请检查格式")

            st.caption(f"系统时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        # 处理设置
        with st.expander("⚡ 处理设置"):
            st.session_state.auto_process = st.checkbox(
                "自动处理模式",
                value=st.session_state.auto_process,
                help="开启后，输入文本变化将自动触发转换"
            )

            # 优化批处理大小
            st.session_state.batch_size = st.slider(
                "每批处理记录数",
                min_value=1,
                max_value=10,  # 增加最大批量大小
                value=5,  # 默认值设为5
                help="较小的批量大小可提高成功率，建议3-5条"
            )

            # 模型选择
            st.session_state.model_version = st.selectbox(
                "模型版本",
                options=["deepseek-chat", "deepseek-coder"],
                index=0,
                help="聊天模型适合自然语言，编程模型适合结构化数据"
            )

            # 调试模式
            st.session_state.debug_mode = st.checkbox(
                "调试模式",
                value=st.session_state.debug_mode,
                help="显示API原始响应，用于问题排查"
            )

        # API端点设置
        with st.expander("🌐 API端点设置"):
            endpoint_options = {
                "官方主端点(v1)": "https://api.deepseek.com/v1/chat/completions",
                "官方主端点(旧版)": "https://api.deepseek.com/chat/completions",
                "备用端点": "https://api.deepseek.cc/chat/completions",
                "国内优化端点": "https://api.deepseek.com.cn/chat/completions"
            }

            selected_endpoint = st.selectbox(
                "选择API端点:",
                list(endpoint_options.keys()),
                index=0
            )
            st.session_state.api_endpoint = endpoint_options[selected_endpoint]

            if st.button("测试连接", key="test_connection"):
                test_api_connection()

        # 缓存管理
        with st.expander("💾 缓存管理"):
            if st.button("🧹 清除API缓存", help="清除缓存的API响应结果"):
                st.session_state.cache_dict = {}
                st.success("缓存已清除！")

            if st.button("🧹 清除已处理记录", help="清除已处理的记录列表"):
                st.session_state.processed_records = set()
                st.success("已清除已处理记录列表！")

            st.info(f"当前缓存数量: {len(st.session_state.cache_dict)}")
            st.info(f"已处理记录数: {len(st.session_state.processed_records)}")

            if st.session_state.auto_save_counter > 0:
                save_time = datetime.now().strftime("%H:%M:%S")
                st.success(f"⏱️ 自动保存于: {save_time} (已保存{st.session_state.auto_save_counter}次)")

        # 性能统计
        if 'api_response_time' in st.session_state and st.session_state.api_call_count > 0:
            with st.expander("📊 性能统计"):
                st.info(f"API调用次数: {st.session_state.api_call_count}次")
                st.info(f"平均响应时间: {st.session_state.api_response_time:.2f}秒")
                total_time = st.session_state.api_response_time * st.session_state.api_call_count
                st.info(f"总处理时间: {total_time:.2f}秒")
                if total_time > 10:
                    st.warning("处理时间超过10秒，请尝试减小批量大小")

        # 使用说明
        with st.expander("❓ 使用帮助", expanded=True):
            st.markdown("""
            **API密钥设置步骤:**
            1. 在DeepSeek官网申请API密钥
            2. 复制完整密钥（以`sk-`开头，长度35字符）
            3. 在左侧"API密钥设置"区域粘贴密钥
            4. 点击"保存密钥"按钮

            **密钥格式要求:**
            - 必须以`sk-`开头
            - 长度35字符
            - 不要包含多余空格

            **正确的输入格式示例:**
            ```
            融创 凡尔赛领馆四期 16栋27-7 15223355185 空调内外机清洗 1 380 未支付 有异味，需要全拆洗
            华宇 寸滩派出所楼上 2栋9-8 13983014034 挂机加氟 1 299 未支付 周末上门
            ```

            **字段说明:**
            1. **记录**: 用户自行填写（空）
            2. **物业**: 物业名称（用户自行填写）
            3. **地址**: 服务地址（必填）
            4. **房号**: 格式为XX-XX-XX或XX-XX（必填）
            5. **联系方式**: 11位手机号码（必填）
            6. **清洗内容**: 具体服务内容描述（必填）
            7. **数量**: 服务数量（默认1）
            8. **金额**: 服务费用（必填）
            9. **付款方式**: 付款方式（未支付/已支付）
            10. **备注**: 其他备注信息（可选）

            **常见问题解决:**
            - ❌ 密钥无效：重新申请并完整复制
            - 🔒 连接失败：尝试切换API端点
            - 🕒 请求超时：减小批量处理大小
            - 🔁 重复记录：已自动过滤已处理的记录
            - ❌ 记录串行：确保使用标准格式
            - 🔍 识别错误：检查字段名称是否标准

            **高级技巧:**
            - 使用空格分隔多个字段
            - 每行一条完整记录
            - 在"备注"中可添加额外信息
            """)

        # 页脚
        st.divider()
        st.caption("© 2025 清洗服务记录转换工具 | 增强版 v8.0")


# === 主应用界面 ===
def main_app():
    # 确保会话状态初始化
    initialize_session_state()
    check_dependencies()

    # 安全获取API密钥
    api_key, key_sources = get_deepseek_api_key()

    st.title("🧹 清洗服务记录转换工具")
    st.markdown("""
    将无序繁杂的清洗服务记录文本转换为结构化的表格数据，并导出为Excel文件。
    **支持1-50行数据处理**，**处理时间控制在10秒内**。
    """)

    # 示例文本 - 更新为新的表头结构
    sample_text = """
融创 凡尔赛领馆四期 16栋27-7 15223355185 空调内外机清洗 1 380 未支付 有异味，需要全拆洗

华宇 寸滩派出所楼上 2栋9-8 13983014034 挂机加氟 1 299 未支付 周末上门

龙湖源著 8栋12-3 13800138000 空调维修 1 200 已支付 不制冷

恒大御景半岛 3栋2单元501 13512345678 中央空调深度清洗 1 380 已支付 业主周日下午在家
    """.strip()

    # 创建输入区域
    with st.expander("📝 输入清洗服务记录文本 (支持1-50行)", expanded=True):
        # 确保input_text已初始化
        if 'input_text' not in st.session_state:
            st.session_state.input_text = sample_text

        input_text = st.text_area("请输入清洗服务记录（每行一条记录）:",
                                  value=st.session_state.input_text,
                                  height=300,
                                  placeholder="请输入清洗服务记录文本...",
                                  key="input_text_area",
                                  help="每行一条完整记录，使用空格分隔字段")

        # 创建按钮行
        col1, col2, col3 = st.columns([1, 1, 2])
        with col1:
            # 添加示例下载按钮
            st.download_button("📥 下载示例文本",
                               sample_text,
                               file_name="清洗服务记录示例.txt",
                               help="下载标准格式的示例文本",
                               use_container_width=True)
        with col2:
            # 添加保存文本按钮
            if st.button("💾 保存当前文本", key="save_text_button", use_container_width=True):
                st.session_state.input_text = input_text
                st.success("文本已保存！")
        with col3:
            st.info("💡 提示：每行一条记录，字段间用空格分隔")

    # 创建处理按钮行
    process_col1, process_col2, process_col3 = st.columns([1, 1, 2])
    with process_col1:
        process_clicked = st.button("🚀 转换文本为表格", use_container_width=True, key="convert_button",
                                    disabled=not st.session_state.api_key_saved)

        if not st.session_state.api_key_saved:
            st.warning("请先保存API密钥")

        if process_clicked:
            # 使用包装函数避免递归问题
            asyncio.run(async_process_records(input_text))

    with process_col3:
        if st.button("🔄 从缓存恢复数据", use_container_width=True, key="restore_button"):
            if 'cached_df' in st.session_state and not st.session_state.cached_df.empty:
                st.session_state.df = st.session_state.cached_df
                st.success("已从缓存恢复数据！")
            else:
                st.warning("没有找到缓存数据")

    # 表格显示区域 - 在输入区下方
    if 'df' in st.session_state and isinstance(st.session_state.df, pd.DataFrame) and not st.session_state.df.empty:
        display_results()
    else:
        st.info("👆 请在上方输入文本并点击'转换文本为表格'按钮")

        # 显示正确的表格结构示例 - 使用新的表头
        st.subheader("正确表格结构示例")
        example_df = pd.DataFrame({
            "记录": ["", ""],
            "物业": ["融创", "华宇"],
            "地址": ["凡尔赛领馆四期", "寸滩派出所楼上"],
            "房号": ["16栋27-7", "2栋9-8"],
            "联系方式": ["15223355185", "13983014034"],
            "清洗内容": ["空调内外机清洗", "挂机加氟"],
            "数量": ["1", "1"],
            "金额": ["380", "299"],
            "付款方式": ["未支付", "未支付"],
            "备注": ["有异味，需要全拆洗", "周末上门"]
        })
        st.dataframe(example_df)

    # 显示已处理记录数统计
    if st.session_state.processed_records:
        st.info(f"已处理记录数: {len(st.session_state.processed_records)}")
    else:
        st.info("尚未处理任何记录")

    # 使用说明
    st.divider()
    st.subheader("使用说明")
    st.markdown("""
    1. 在文本框中输入清洗服务记录（每行一条记录）
    2. 点击 **🚀 转换文本为表格** 按钮
    3. 查看解析后的表格数据
    4. 点击 **⬇️ 下载Excel文件** 导出数据

    ### 支持的文本格式示例:
    融创 凡尔赛领馆四期 16栋27-7 15223355185 空调内外机清洗 1 380 未支付 有异味，需要全拆洗

    华宇 寸滩派出所楼上 2栋9-8 13983014034 挂机加氟 1 299 未支付 周末上门

    龙湖源著 8栋12-3 13800138000 空调维修 1 200 已支付 不制冷

    ### 解析规则:
    1. 自动识别11位电话号码
    2. 识别"未支付"和"已支付"状态
    3. 提取金额信息（如380元）
    4. 识别房号格式（如16栋27-7）
    5. 开头的项目名称作为物业
    6. 剩余内容分割为清洗内容和其他信息

    **性能优化:**
    - 支持1-50行数据处理
    - 处理时间控制在10秒内
    - 自动分批处理提高效率
    """)

    # 页脚
    st.divider()
    st.caption("© 2025 清洗服务记录转换工具 | 使用Python和Streamlit构建")


# === 应用入口 ===
if __name__ == "__main__":
    try:
        # 确保在任何操作前初始化session state
        initialize_session_state()

        # 运行应用
        sidebar_config()
        main_app()
    except Exception as e:
        st.error(f"应用发生错误: {str(e)}")
        logger.exception("应用错误")