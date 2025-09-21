import json
import logging
import hashlib
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler
from utils.api import call_danmaku_api
from utils.permission import check_user_permission
from utils.conversation_states import EPISODES_PER_PAGE, INPUT_EPISODE_RANGE, CALLBACK_DATA_MAX_LEN, IMPORT_AUTO_KEYWORD_INPUT, IMPORT_AUTO_ID_INPUT

# 初始化日志
logger = logging.getLogger(__name__)


@check_user_permission
async def handle_import_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理「导入按钮」的回调事件（direct_import核心逻辑）"""
    query = update.callback_query
    logger.info(f"📥 收到导入回调数据：{query.data}")
    
    # 1. 解析回调数据
    try:
        callback_data = json.loads(query.data)
        action = callback_data.get("action")
        result_index = callback_data.get("result_index")
        
        if action != "import_media" or result_index is None:
            await query.answer("❌ 无效的操作请求", show_alert=True)
            return
    except json.JSONDecodeError:
        await query.answer("❌ 数据解析失败，请重试", show_alert=True)
        return

    # 2. 读取上下文保存的searchId
    search_id = context.user_data.get("search_id", "")
    if not search_id:
        await query.answer("❌ 未找到历史搜索记录，请重新搜索", show_alert=True)
        return

    # 3. 按钮加载状态提示（已注释，根据用户要求不影响按钮展示）
    # await query.answer("🔄 正在发起导入请求...", show_alert=False)

    # 4. 调用API执行direct_import
    api_result = call_danmaku_api(
        method="POST",
        endpoint="/import/direct",
        json_data={
            "searchId": search_id,
            "result_index": result_index,
        }
    )

    # 5. 处理导入结果
    if api_result["success"]:
        data = api_result["data"]
        # 发送结果通知
        await query.message.reply_text(f"""
🎉 导入请求已提交成功！
• 任务ID：{data.get('taskId', '无')}
        """.strip())
    else:
        # 发送失败原因
        await query.message.reply_text(f"""
❌ 导入失败：{api_result['error']}
• 建议：若多次失败，可尝试重新搜索后导入
        """.strip())


async def handle_import_auto_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理import_auto相关的回调"""
    query = update.callback_query
    logger.info(f"🔍 收到import_auto回调: {query.data}")
    await query.answer()
    
    # 首先检查是否为季度选择回调（非JSON格式）
    if query.data.startswith("season_") or query.data == "cancel":
        logger.info(f"📺 处理季度选择回调: {query.data}")
        # 处理季度选择回调
        from handlers.import_media import import_auto_season_selection
        return await import_auto_season_selection(update, context)
    
    # 检查是否为非import_auto相关的回调数据，直接返回
    if not query.data.startswith('{"action": "import_auto'):
        logger.info(f"🔄 非import_auto回调数据，跳过处理: {query.data}")
        return ConversationHandler.END
    
    # 尝试解析JSON格式的回调数据
    try:
        callback_data = json.loads(query.data)
        action = callback_data.get("action")
        
        if action == "import_auto_search_type":
            return await handle_search_type_selection(update, context, callback_data)
        elif action == "import_auto_media_type":
            return await handle_media_type_selection(update, context, callback_data)
        # elif action == "import_auto_method":
        #     return await handle_import_method_selection(update, context, callback_data)
        # elif action == "continue_season_import":
        #     return await handle_continue_season_import(update, context)
        # elif action == "continue_episode_import":
        #     return await handle_continue_episode_import(update, context, callback_data)
        elif action == "finish_import":
            return await handle_finish_import(update, context)
        else:
            await query.edit_message_text("❌ 未知的操作类型")
            return ConversationHandler.END
            
    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"解析import_auto回调数据失败: {e}")
        await query.answer("❌ 无效的回调数据")
        return ConversationHandler.END


async def handle_search_type_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, callback_data: dict):
    """处理搜索类型选择"""
    query = update.callback_query
    search_type = callback_data.get("type")
    
    # 保存搜索类型到上下文
    context.user_data["import_auto_search_type"] = search_type
    
    if search_type == "keyword":
        # 关键词搜索：直接提示输入关键词
        await query.edit_message_text(
            "🔍 **关键词搜索**\n\n"
            "请输入影视作品名称：\n\n"
            "💡 **智能功能**：\n"
            "• 自动识别电影/电视剧类型\n"
            "• 电视剧自动提供季度选择\n"
            "• 支持中英文搜索"
        )
        return IMPORT_AUTO_KEYWORD_INPUT
    else:
        # 平台ID搜索：直接提示输入ID
        platform_names = {
            "tmdb": "TMDB",
            "tvdb": "TVDB", 
            "douban": "豆瓣",
            "imdb": "IMDB",
            "bangumi": "Bangumi"
        }
        platform_name = platform_names.get(search_type, search_type.upper())
        
        await query.edit_message_text(
            f"🆔 **{platform_name} ID搜索**\n\n请输入{platform_name} ID或链接："
        )
        
        return IMPORT_AUTO_ID_INPUT


async def handle_media_type_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, callback_data: dict):
    """处理媒体类型选择（用于关键词搜索和平台ID搜索）"""
    query = update.callback_query
    media_type = callback_data.get("type")
    
    # 保存媒体类型到上下文
    context.user_data["import_auto_media_type"] = media_type
    
    type_names = {"tv_series": "电视剧/动漫", "movie": "电影"}
    type_name = type_names.get(media_type, media_type)
    
    # 检查是否已有关键词（来自 /search 命令）
    existing_keyword = context.user_data.get("import_auto_keyword")
    if existing_keyword:
        # 已有关键词，根据媒体类型决定流程
        if media_type == "movie":
            # 电影类型：直接导入
            await query.edit_message_text(
                f"✅ 已选择：{type_name}\n关键词：{existing_keyword}"
            )
            
            import_params = {
                "searchType": "keyword",
                "searchTerm": existing_keyword,
                "mediaType": media_type,
                "importMethod": "auto",
                "originalKeyword": existing_keyword  # 保存原始关键词用于识别词匹配
            }
            
            from handlers.import_media import call_import_auto_api
            await call_import_auto_api(update, context, import_params)
            return ConversationHandler.END
        else:
            # 电视剧类型：显示导入方式选择
            await query.edit_message_text(
                f"✅ 已选择：{type_name}\n关键词：{existing_keyword}\n\n请选择导入方式："
            )
            
            # 保存导入参数
            context.user_data["import_auto_params"] = {
                "searchType": "keyword",
                "searchTerm": existing_keyword,
                "mediaType": media_type,
                "originalKeyword": existing_keyword  # 保存原始关键词用于识别词匹配
            }
            
            # 显示导入方式选择
            from handlers.import_media import show_import_options
            return await show_import_options(update, context, context.user_data["import_auto_params"])
    
    # 检查是否已有平台ID
    existing_id = context.user_data.get("import_auto_id")
    if existing_id:
        # 已有平台ID，根据媒体类型决定流程
        search_type = context.user_data.get("import_auto_search_type", "tmdb")
        auto_detected_type = context.user_data.get("import_auto_media_type")
        
        # 检查是否与自动检测的类型不一致
        if auto_detected_type and auto_detected_type != media_type:
            detected_name = "电影" if auto_detected_type == "movie" else "电视剧/动漫"
            await query.edit_message_text(
                f"⚠️ **类型不一致提醒**\n\n"
                f"🔍 自动检测: {detected_name}\n"
                f"👤 用户选择: {type_name}\n\n"
                f"将按用户选择的类型进行导入。\n\n"
                f"{'✅ 电影类型确认' if media_type == 'movie' else '请选择导入方式：'}"
            )
        else:
            await query.edit_message_text(
                f"✅ 已选择：{type_name}\nID：{existing_id}\n\n"
                f"{'✅ 电影类型确认' if media_type == 'movie' else '请选择导入方式：'}"
            )
        
        if media_type == "movie":
            # 电影类型：直接导入
            import_params = {
                "searchType": search_type,
                "searchTerm": existing_id,
                "mediaType": media_type,
                "importMethod": "auto",
                "originalKeyword": context.user_data.get("import_auto_keyword", "")  # 保存原始关键词用于识别词匹配
            }
            
            from handlers.import_media import call_import_auto_api
            await call_import_auto_api(update, context, import_params)
            return ConversationHandler.END
        else:
            # 电视剧类型：显示导入方式选择
            context.user_data["import_auto_params"] = {
                "searchType": search_type,
                "searchTerm": existing_id,
                "mediaType": media_type,
                "originalKeyword": context.user_data.get("import_auto_keyword", "")  # 保存原始关键词用于识别词匹配
            }
            
            from handlers.import_media import show_import_options
            return await show_import_options(update, context, context.user_data["import_auto_params"])
    
    # 既没有关键词也没有ID，这种情况不应该发生
    await query.edit_message_text(
        "❌ 系统错误：缺少搜索内容，请重新开始。"
    )
    return ConversationHandler.END


async def handle_search_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """专门用于ConversationHandler的搜索类型选择回调处理器"""
    query = update.callback_query
    await query.answer()
    
    try:
        callback_data = json.loads(query.data)
        if callback_data.get("action") == "import_auto_search_type":
            return await handle_search_type_selection(update, context, callback_data)
    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"解析搜索类型回调数据失败: {e}")
        await query.answer("❌ 无效的回调数据")
    
    return ConversationHandler.END


async def handle_media_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """专门用于ConversationHandler的媒体类型选择回调处理器"""
    query = update.callback_query
    await query.answer()
    
    try:
        callback_data = json.loads(query.data)
        if callback_data.get("action") == "import_auto_media_type":
            return await handle_media_type_selection(update, context, callback_data)
    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"解析媒体类型回调数据失败: {e}")
        await query.answer("❌ 无效的回调数据")
    
    return ConversationHandler.END


# 已移除handle_import_method_selection函数，因为不再需要导入方式选择


@check_user_permission
async def handle_get_episode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    logger.info(f"📥 收到分集回调数据：{query.data}")
    # 加载状态提示（已注释，根据用户要求不影响按钮展示）
    # await query.answer("处理中...", show_alert=False)

    try:
        # ------------------------------
        # 1. 修复：解析回调数据（兼容压缩后的短ID）
        # ------------------------------
        try:
            # 解析回调数据（兼容新旧格式）
            callback_data = json.loads(query.data)
            # 支持新格式（短字段名）和旧格式（完整字段名）
            action = callback_data.get("a") or callback_data.get("action")
            data_id = callback_data.get("d") or callback_data.get("data_id")
            current_page = int(callback_data.get("p", callback_data.get("current_page", 1)))
            logger.info(f"🔍 解析回调数据 - action: '{action}', data_id: '{data_id}', current_page: {current_page}")
            logger.info(f"🔍 原始回调数据: {query.data}")
        except (json.JSONDecodeError, ValueError, TypeError):
            await query.answer("❌ 操作已过期，请重新获取分集", show_alert=True)
            return ConversationHandler.END

        # 校验核心参数
        valid_actions = ["get_media_episode", "get_episodes", "switch_episode_page", "start_input_range"]
        if action not in valid_actions or not data_id:
            await query.answer("❌ 无效操作，请重新获取分集", show_alert=True)
            return ConversationHandler.END

        # ------------------------------
        # 2. 初始化上下文缓存（新增：短ID与原始数据的映射）
        # ------------------------------
        # 缓存结构：
        # context.user_data["episode_data_map"] = {
        #     "短ID": {
        #         "result_index": 原始result_index,
        #         "search_id": 原始search_id,
        #         "total_episodes": 总集数,
        #         "cached_episodes": 全量分集列表
        #     }
        # }
        if "episode_data_map" not in context.user_data:
            context.user_data["episode_data_map"] = {}
        episode_data_map = context.user_data["episode_data_map"]

        # 从短ID映射中获取原始数据（无则提示重新获取）
        if data_id not in episode_data_map and action != "get_media_episode":
            await query.answer("❌ 数据已过期，请重新获取分集", show_alert=True)
            return ConversationHandler.END

        # ------------------------------
        # 3. 首次获取分集：调用接口+生成短ID（核心修复：避免长数据）
        # ------------------------------
        if action == "get_media_episode":
            # 首次获取时，data_id暂存原始result_index（用于生成短ID）
            try:
                result_index = int(data_id)
                search_id = context.user_data.get("search_id", "")
                logger.info(f"🔍 获取分集请求 - result_index: {result_index}, search_id: {search_id}")
                logger.info(f"🔍 当前用户数据: {list(context.user_data.keys())}")
                if not search_id:
                    logger.warning(f"❌ 未找到search_id，用户数据: {context.user_data}")
                    await query.answer("❌ 未找到搜索记录，请重新搜索", show_alert=True)
                    return ConversationHandler.END
            except ValueError:
                await query.answer("❌ 无效参数，请重新获取分集", show_alert=True)
                return ConversationHandler.END

            # 用户要求：点击分集导入时不显示加载状态，保留原按钮状态
            # 注释掉加载状态更新逻辑
            # try:
            #     loading_keyboard = [[InlineKeyboardButton(text="⏳ 加载分集中...", callback_data="empty")]]
            #     await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(loading_keyboard))
            # except BadRequest as e:
            #     logger.warning(f"⚠️ 编辑加载按钮失败：{str(e)}")

            # 调用接口获取全量分集
            logger.info(f"🌐 调用API获取分集 - searchId: {search_id}, result_index: {result_index}")
            api_result = call_danmaku_api(
                method="GET",
                endpoint="/episodes",
                params={"searchId": search_id, "result_index": result_index}
            )
            logger.info(f"🌐 API响应: success={api_result.get('success')}, error={api_result.get('error', 'None')}")
            if api_result.get('success'):
                episodes_count = len(api_result.get('data', []))
                logger.info(f"🌐 获取到 {episodes_count} 个分集数据")

            # 处理接口响应
            if not api_result.get("success", False):
                error_msg = api_result.get("error", "未知错误")
                # 生成重新获取的短回调（使用原始result_index作为临时data_id）
                retry_callback = json.dumps({
                    "action": "get_media_episode",
                    "data_id": str(result_index)  # 临时用result_index，首次获取后替换为短ID
                }, ensure_ascii=False)
                # 校验回调长度（避免再次报错）
                if len(retry_callback) > CALLBACK_DATA_MAX_LEN:
                    retry_callback = json.dumps({"action": "get_media_episode", "data_id": "retry"}, ensure_ascii=False)

                # 保留原有的两个按钮
                fail_keyboard = [
                    [
                        InlineKeyboardButton(text="🔗 立即导入", callback_data=json.dumps({"action": "import_media", "result_index": result_index}, ensure_ascii=False)),
                        InlineKeyboardButton(text="📺 分集导入", callback_data=json.dumps({"action": "get_media_episode", "result_index": result_index}, ensure_ascii=False))
                    ]
                ]
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(fail_keyboard))
                await query.message.reply_text(f"❌ 分集获取失败：{error_msg}")
                return ConversationHandler.END

            # 过滤无效分集（新结构必传字段）
            logger.info(f"🔍 开始过滤分集数据，原始数据数量: {len(api_result.get('data', []))}")
            full_episodes = [
                ep for ep in api_result.get("data", [])
                if all(key in ep for key in ["provider", "episodeId", "title", "episodeIndex"])
            ]
            logger.info(f"🔍 过滤后有效分集数量: {len(full_episodes)}")
            if not full_episodes:
                logger.warning(f"⚠️ 没有有效分集数据")
                await query.message.reply_text("❌ 当前媒体无可用分集数据")
                # 生成重新获取的短回调
                retry_callback = json.dumps({
                    "action": "get_media_episode",
                    "data_id": str(result_index)
                }, ensure_ascii=False)
                # 保留原有的两个按钮
                empty_keyboard = [
                    [
                        InlineKeyboardButton(text="🔗 立即导入", callback_data=json.dumps({"action": "import_media", "result_index": result_index}, ensure_ascii=False)),
                        InlineKeyboardButton(text="📺 分集导入", callback_data=json.dumps({"action": "get_media_episode", "result_index": result_index}, ensure_ascii=False))
                    ]
                ]
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(empty_keyboard))
                return ConversationHandler.END

            # 核心修复：生成短ID（替代长result_index+searchId，减少回调长度）
            # 用searchId+result_index生成MD5，取前8位作为短ID（冲突概率极低）
            raw_data = f"{search_id}_{result_index}"
            short_id = hashlib.md5(raw_data.encode()).hexdigest()[:8]
            logger.info(f"🔑 生成短ID: {short_id}，原始数据: {raw_data}")
            
            # 缓存原始数据到短ID映射
            episode_data_map[short_id] = {
                "result_index": result_index,
                "search_id": search_id,
                "total_episodes": len(full_episodes),
                "cached_episodes": full_episodes
            }
            logger.info(f"💾 缓存分集数据到短ID映射，总集数: {len(full_episodes)}")
            
            # 更新data_id为短ID（后续操作使用）
            data_id = short_id
            logger.info(f"🔄 更新data_id为短ID: {data_id}")
            
            # 直接显示分集列表（用户要求的优化）
            logger.info(f"📋 直接显示分集列表，跳过中间选择步骤")
            
            # 计算分页参数（第一页）
            current_page = 1
            total_pages = (len(full_episodes) + EPISODES_PER_PAGE - 1) // EPISODES_PER_PAGE
            start_idx = 0
            end_idx = EPISODES_PER_PAGE
            current_page_episodes = full_episodes[start_idx:end_idx]
            
            # 构建分集详情
            page_info = f"（第{current_page}/{total_pages}页）" if total_pages > 1 else ""
            episode_details = []
            for i, episode in enumerate(current_page_episodes, 1):
                provider = episode.get("provider", "未知来源")
                episode_index = episode["episodeIndex"]
                episode_title = episode.get("title", f"第{episode_index}集")
                episode_details.append(f"{i}. 【第{episode_index}集】{episode_title} ({provider})")
            
            episodes_text = "\n".join(episode_details)
            full_message = f"""✅ 共找到 {len(full_episodes)} 集有效分集 {page_info}
💡 请直接输入集数区间或 all：
   • 单集：1
   • 区间：1-10
   • 多选：1,5,10
   • 全部：all

📺 分集列表：
{episodes_text}"""
            
            # 生成操作按钮
            buttons = []
            
            # 分页按钮行（仅在多页时显示）
            if total_pages > 1:
                pagination_buttons = []
                # 上一页按钮
                if current_page > 1:
                    prev_callback = json.dumps({
                        "a": "switch_episode_page",
                        "d": data_id,
                        "p": current_page - 1
                    }, ensure_ascii=False)
                    if len(prev_callback) > CALLBACK_DATA_MAX_LEN:
                        safe_id_len = 17
                        prev_callback = json.dumps({
                            "a": "switch_episode_page",
                            "d": data_id[:safe_id_len],
                            "p": current_page - 1
                        }, ensure_ascii=False)
                    pagination_buttons.append(InlineKeyboardButton(text="⬅️ 上一页", callback_data=prev_callback))

                # 下一页按钮
                if current_page < total_pages:
                    next_callback = json.dumps({
                        "a": "switch_episode_page",
                        "d": data_id,
                        "p": current_page + 1
                    }, ensure_ascii=False)
                    if len(next_callback) > CALLBACK_DATA_MAX_LEN:
                        safe_id_len = 17
                        next_callback = json.dumps({
                            "a": "switch_episode_page",
                            "d": data_id[:safe_id_len],
                            "p": current_page + 1
                        }, ensure_ascii=False)
                    pagination_buttons.append(InlineKeyboardButton(text="下一页 ➡️", callback_data=next_callback))
                
                if pagination_buttons:
                    buttons.append(pagination_buttons)
            
            # 不再添加输入按钮和导入按钮，用户可以直接输入
            
            # 存储当前数据ID供输入处理使用
            context.user_data["current_data_id"] = data_id
            
            logger.info(f"📤 发送分集列表消息，总集数: {len(full_episodes)}, 当前页: {current_page}/{total_pages}")
            await query.message.reply_text(
                text=full_message,
                reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
                parse_mode=None
            )
            logger.info(f"✅ 分集列表消息发送成功，进入集数输入状态")
            return INPUT_EPISODE_RANGE

        # ------------------------------
        # 4. 分页预览逻辑（使用短ID获取原始数据）
        # ------------------------------
        # 从短ID映射中获取原始数据
        current_data = episode_data_map.get(data_id, {})
        full_episodes = current_data.get("cached_episodes", [])
        total_episodes = current_data.get("total_episodes", 0)
        if not full_episodes or total_episodes == 0:
            await query.answer("❌ 数据已过期，请重新获取分集", show_alert=True)
            return ConversationHandler.END

        # ------------------------------
        # 5. 触发集数输入流程（直接处理，不显示分页）
        # ------------------------------
        if action == "start_input_range":
            # 存储当前短ID（供输入处理函数使用）
            context.user_data["current_data_id"] = data_id
            await query.message.reply_text(
                f"📝 请输入需要导入的集数（当前共{total_episodes}集）：\n\n"
                f"💡 **支持格式：**\n"
                f"• 单个集数：如 `5`\n"
                f"• 集数区间：如 `1-10` 或 `5,8,12`\n"
                f"• 全部分集：输入 `all`",
                parse_mode="Markdown"
            )
            return INPUT_EPISODE_RANGE

        # 处理get_episodes动作：直接进入输入状态（用户要求的优化）
        elif action == "get_episodes":
            logger.info(f"📋 处理获取分集请求，直接进入输入状态")
            # 存储当前短ID（供输入处理函数使用）
            context.user_data["current_data_id"] = data_id
            await query.message.reply_text(
                f"📝 请输入需要导入的集数（当前共{total_episodes}集）：\n\n"
                f"💡 **支持格式：**\n"
                f"• 单个集数：如 `5`\n"
                f"• 集数区间：如 `1-10` 或 `5,8,12`\n"
                f"• 全部分集：输入 `all`",
                parse_mode="Markdown"
            )
            return INPUT_EPISODE_RANGE
        
        # 处理分页显示逻辑（仅在翻页时执行）
        elif action == "switch_episode_page":
            logger.info(f"📋 进入分页显示逻辑，action: {action}, data_id: {data_id}")
            logger.info(f"📄 处理翻页请求：切换到第{current_page}页")

            # 计算分页参数
            total_pages = (total_episodes + EPISODES_PER_PAGE - 1) // EPISODES_PER_PAGE
            current_page = max(1, min(current_page, total_pages))  # 修正非法页码
            start_idx = (current_page - 1) * EPISODES_PER_PAGE
            end_idx = start_idx + EPISODES_PER_PAGE
            current_page_episodes = full_episodes[start_idx:end_idx]

            # 4.1 构建分集详情（1条消息显示10个分集）
            page_info = f"（第{current_page}/{total_pages}页）" if total_pages > 1 else ""
            episode_details = []
            for i, episode in enumerate(current_page_episodes, 1):
                provider = episode.get("provider", "未知来源")
                episode_index = episode["episodeIndex"]
                episode_title = episode.get("title", f"第{episode_index}集")
                episode_details.append(f"{i}. 【第{episode_index}集】{episode_title} ({provider})")
            
            episodes_text = "\n".join(episode_details)
            # 4.2 生成分页和输入按钮（按需显示）
            buttons = []
            
            # 分页按钮行（仅在多页时显示）
            if total_pages > 1:
                pagination_buttons = []
                # 上一页按钮（使用短字段名）
                if current_page > 1:
                    prev_callback = json.dumps({
                        "a": "switch_episode_page",  # action缩写
                        "d": data_id,  # data_id缩写
                        "p": current_page - 1  # current_page缩写
                    }, ensure_ascii=False)
                    # 回调长度校验和截断处理
                    if len(prev_callback) > CALLBACK_DATA_MAX_LEN:
                        logger.warning(f"⚠️ 上一页回调过长({len(prev_callback)})，截断data_id")
                        # 计算安全的data_id长度
                        safe_id_len = max(4, 17)  # 基于测试结果，分页按钮最多17字符
                        prev_callback = json.dumps({
                            "a": "switch_episode_page",
                            "d": data_id[:safe_id_len],
                            "p": current_page - 1
                        }, ensure_ascii=False)
                        logger.info(f"✅ 截断后回调长度：{len(prev_callback)}")
                    pagination_buttons.append(InlineKeyboardButton(text="⬅️ 上一页", callback_data=prev_callback))

                # 移除页码显示按钮，优化界面简洁性

                # 下一页按钮（使用短字段名）
                if current_page < total_pages:
                    next_callback = json.dumps({
                        "a": "switch_episode_page",
                        "d": data_id,
                        "p": current_page + 1
                    }, ensure_ascii=False)
                    if len(next_callback) > CALLBACK_DATA_MAX_LEN:
                        logger.warning(f"⚠️ 下一页回调过长({len(next_callback)})，截断data_id")
                        safe_id_len = max(4, 17)  # 分页按钮安全长度
                        next_callback = json.dumps({
                            "a": "switch_episode_page",
                            "d": data_id[:safe_id_len],
                            "p": current_page + 1
                        }, ensure_ascii=False)
                        logger.info(f"✅ 截断后回调长度：{len(next_callback)}")
                    pagination_buttons.append(InlineKeyboardButton(text="下一页 ➡️", callback_data=next_callback))
                
                buttons.append(pagination_buttons)
            
            # 移除输入集数区间和立即导入全部按钮（用户要求的优化）
            # 分页显示时只保留分页按钮，用户可直接输入集数
            
            full_message = f"""✅ 共找到 {total_episodes} 集有效分集 {page_info}

💡 **支持输入格式：**
• 单个集数：如 `5`
• 集数区间：如 `1-10` 或 `5,8,12`
• 全部分集：输入 `all`

📺 分集列表：
{episodes_text}"""
            
            # 发送分集列表消息和按钮（一次性发送）
            keyboard = InlineKeyboardMarkup(buttons) if buttons else None
            logger.info(f"📤 发送分集列表消息，总集数: {total_episodes}, 当前页: {current_page}/{total_pages}, 按钮数量: {len(buttons)}")
            await query.edit_message_text(
                text=full_message,
                reply_markup=keyboard,
                parse_mode=None
            )
            logger.info(f"✅ 分集列表消息和按钮发送成功")
            # 保持会话状态，允许用户直接输入集数
            context.user_data["current_data_id"] = data_id
            return INPUT_EPISODE_RANGE

    except BadRequest as e:
        # 捕获Telegram按钮相关错误（如Button_data_invalid）
        logger.error(f"❌ 按钮回调错误：{str(e)}（当前回调长度：{len(query.data) if query.data else 0}）", exc_info=True)
        await query.answer("❌ 操作失败，请重新获取分集", show_alert=True)
        # 恢复基础按钮（使用最短回调）
        if "data_id" in locals():
            try:
                retry_callback = json.dumps({"action": "get_media_episode", "data_id": data_id[:6]}, ensure_ascii=False)
                # 保留原有的两个按钮
                error_keyboard = [
                    [
                        InlineKeyboardButton(text="🔗 立即导入", callback_data=json.dumps({"action": "import_media", "data_id": data_id}, ensure_ascii=False)),
                        InlineKeyboardButton(text="📺 分集导入", callback_data=json.dumps({"action": "get_media_episode", "data_id": data_id}, ensure_ascii=False))
                    ]
                ]
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(error_keyboard))
            except Exception:
                pass
    except Exception as e:
        logger.error(f"❌ 分集处理异常：{str(e)}", exc_info=True)
        await query.answer("❌ 处理失败，请重试", show_alert=True)
        if "data_id" in locals():
            try:
                retry_callback = json.dumps({"action": "get_media_episode", "data_id": data_id[:6]}, ensure_ascii=False)
                # 保留原有的两个按钮
                error_keyboard = [
                    [
                        InlineKeyboardButton(text="🔗 立即导入", callback_data=json.dumps({"action": "import_media", "data_id": data_id}, ensure_ascii=False)),
                        InlineKeyboardButton(text="📺 分集导入", callback_data=json.dumps({"action": "get_media_episode", "data_id": data_id}, ensure_ascii=False))
                    ]
                ]
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(error_keyboard))
            except Exception:
                pass

    return ConversationHandler.END


# ------------------------------
# 集数输入处理（适配短ID）
# ------------------------------
@check_user_permission
async def handle_episode_range_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text.strip()
    logger.info(f"📥 收到集数区间输入：{user_input}")

    # 从短ID映射中获取原始数据（适配修复）
    current_data_id = context.user_data.get("current_data_id")
    episode_data_map = context.user_data.get("episode_data_map", {})
    current_data = episode_data_map.get(current_data_id, {})

    # 校验数据（适配短ID）
    full_episodes = current_data.get("cached_episodes", [])
    total_episodes = current_data.get("total_episodes", 0)
    if not current_data_id or current_data_id not in episode_data_map or not full_episodes:
        await update.message.reply_text("❌ 数据已过期，请重新获取分集")
        return ConversationHandler.END

    # 解析集数（支持all选项）
    episode_index_map = {ep["episodeIndex"]: ep for ep in full_episodes}
    valid_episode_indices = set(episode_index_map.keys())
    
    # 检查是否为全部导入
    if user_input.lower() == 'all':
        selected_indices = valid_episode_indices
        await update.message.reply_text(
            f"✅ 已选择导入全部 {len(selected_indices)} 集\n"
            f"💡 即将开始导入"
        )
    else:
        range_segments = [seg.strip() for seg in user_input.split(",") if seg.strip()]

        if not range_segments:
            await update.message.reply_text("❌ 输入为空，请重新输入\n💡 支持格式：\n• 单个集数：如 5\n• 集数区间：如 1-10 或 5,8,12\n• 全部分集：输入 all")
            return INPUT_EPISODE_RANGE

        selected_indices = set()
        invalid_segments = []
        for seg in range_segments:
            if "-" in seg:
                try:
                    start, end = map(int, [s.strip() for s in seg.split("-", 1)])
                    if start > end:
                        start, end = end, start
                    segment_indices = set(range(start, end + 1))
                except (ValueError, IndexError):
                    invalid_segments.append(seg)
                    continue
            else:
                try:
                    segment_indices = {int(seg)}
                except ValueError:
                    invalid_segments.append(seg)
                    continue

            valid_in_segment = segment_indices & valid_episode_indices
            selected_indices.update(valid_in_segment)
            invalid_in_segment = segment_indices - valid_episode_indices
            if invalid_in_segment:
                invalid_segments.append(f"{seg}（无效集数：{sorted(invalid_in_segment)}）")
            
        if not selected_indices:
            msg = "❌ 未找到有效集数，请重新输入\n"
            if invalid_segments:
                msg += f"无效片段：{', '.join(invalid_segments)}\n"
            msg += f"当前支持集数：1-{total_episodes}\n💡 支持格式：\n• 单个集数：如 5\n• 集数区间：如 1-10 或 5,8,12\n• 全部分集：输入 all"
            await update.message.reply_text(msg)
            return INPUT_EPISODE_RANGE

        # 显示选中结果
        sorted_indices = sorted(selected_indices)
        await update.message.reply_text(
            f"✅ 共选中 {len(sorted_indices)} 集：\n"
            f"选中集数：{', '.join(map(str, sorted_indices))}\n"
            f"💡 即将开始导入"
        )

    # 准备导入
    sorted_indices = sorted(selected_indices)

    # 调用/import/edited接口导入选中的集数
    try:
        # 构建episodes参数：包含选中集数的详细信息
        episodes_to_import = []
        for idx in sorted_indices:
            ep = episode_index_map[idx]
            episodes_to_import.append({
                "provider": ep.get("provider"),
                "episodeId": ep.get("episodeId"),
                "title": ep.get("title"),
                "episodeIndex": ep.get("episodeIndex")
            })
        
        # 获取原始数据用于API调用
        result_index = current_data.get("result_index")
        search_id = current_data.get("search_id")
        
        # 调用/import/edited接口
        api_result = call_danmaku_api(
            method="POST",
            endpoint="/import/edited",
            json_data={
                "searchId": search_id,
                "result_index": result_index,
                "episodes": episodes_to_import
            }
        )
        
        # 处理导入结果
        if api_result.get("success", False):
            data = api_result.get("data", {})
            await update.message.reply_text(
                f"🎉 批量导入请求已提交成功！\n"
                f"• 任务ID：{data.get('taskId', '无')}\n"
                f"• 导入集数：{len(sorted_indices)} 集\n"
            )
        else:
            error_msg = api_result.get("error", "未知错误")
            await update.message.reply_text(
                f"❌ 批量导入失败：{error_msg}\n"
                f"• 建议：若多次失败，可尝试重新获取分集后导入"
            )
    except Exception as e:
        logger.error(f"❌ 批量导入异常：{str(e)}", exc_info=True)
        await update.message.reply_text(
            f"❌ 导入过程中发生异常：{str(e)}\n"
            f"• 建议：请重新获取分集后重试"
        )

    return ConversationHandler.END


# ------------------------------
# 取消输入流程（不变）
# ------------------------------
@check_user_permission
async def cancel_episode_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🛑 已取消集数输入")
    # 清空临时数据
    for key in ["current_result_index", "total_episodes"]:
        if key in context.user_data:
            del context.user_data[key]
    return ConversationHandler.END


# ------------------------------
# 继续导入相关处理函数
# ------------------------------
# 已移除handle_continue_season_import和handle_continue_episode_import函数，因为不再需要分季导入和分集导入功能


@check_user_permission
async def handle_finish_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理完成导入"""
    query = update.callback_query
    await query.edit_message_text("✅ 导入流程已完成！")
    
    # 清理上下文数据
    keys_to_clear = [
        "import_auto_params", "import_auto_season", "import_auto_episode",
        "import_auto_episode_mode"
    ]
    for key in keys_to_clear:
        context.user_data.pop(key, None)
    
    return ConversationHandler.END





async def show_paged_results(update_or_query, context, search_results, page=0, per_page=5):
    """显示分页的搜索结果"""
    results_per_page = per_page  # 每页显示结果数量
    search_id = context.user_data.get("search_id", "")
    total_pages = (len(search_results) + results_per_page - 1) // results_per_page
    
    # 计算当前页的结果范围
    start_idx = page * results_per_page
    end_idx = min(start_idx + results_per_page, len(search_results))
    current_results = search_results[start_idx:end_idx]
    
    # 保存分页信息到上下文
    context.user_data["search_page"] = page
    context.user_data["search_total_pages"] = total_pages
    
    # 发送当前页的结果
    for idx, item in enumerate(current_results):
        actual_idx = start_idx + idx
        result_text = f"""【{actual_idx + 1}/{len(search_results)}】{item.get('title', '未知名称')}
• 类型：{item.get('type', '未知类型')} | 来源：{item.get('provider', '未知来源')}
• 年份：{item.get('year', '未知年份')} | 季度：{item.get('season', '未知季度')}
• 总集数：{item.get('episodeCount', '0')}集"""
        
        # 构造回调数据
        callback_data_import = json.dumps({
            "action": "import_media",
            "result_index": actual_idx
        }, ensure_ascii=False)
        
        callback_data_episode = json.dumps({
            "action": "get_media_episode",
            "data_id": str(actual_idx)
        }, ensure_ascii=False)
        
        # 生成内联键盘
        keyboard = [
            [InlineKeyboardButton(
                text="🔗 立即导入",
                callback_data=callback_data_import
            ),
            InlineKeyboardButton(
                text="📺 分集导入",
                callback_data=callback_data_episode
            )]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # 发送结果消息
        if hasattr(update_or_query, 'message'):  # 这是一个Update对象
            await update_or_query.message.reply_text(
                text=result_text,
                reply_markup=reply_markup,
                parse_mode=None
            )
        else:  # 这是一个CallbackQuery对象
            await update_or_query.message.reply_text(
                text=result_text,
                reply_markup=reply_markup,
                parse_mode=None
            )
    
    # 发送分页控制消息
    page_text = f"📄 第 {page + 1}/{total_pages} 页 | 共 {len(search_results)} 个结果"
    page_keyboard = []
    
    # 分页按钮
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(
            "⬅️ 上一页",
            callback_data=json.dumps({"action": "search_page", "page": page - 1}, ensure_ascii=False)
        ))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(
            "下一页 ➡️",
            callback_data=json.dumps({"action": "search_page", "page": page + 1}, ensure_ascii=False)
        ))
    if nav_row:
        page_keyboard.append(nav_row)
    
    page_reply_markup = InlineKeyboardMarkup(page_keyboard) if page_keyboard else None
    
    # 发送分页控制消息
    if hasattr(update_or_query, 'message'):  # 这是一个Update对象
        if page_reply_markup:
            await update_or_query.message.reply_text(
                text=page_text,
                reply_markup=page_reply_markup
            )
        else:
            await update_or_query.message.reply_text(text=page_text)
    else:  # 这是一个CallbackQuery对象
        try:
            await update_or_query.edit_message_text(
                text=page_text,
                reply_markup=page_reply_markup
            )
        except Exception as e:
            logger.error(f"编辑分页消息失败: {e}")
            if page_reply_markup:
                await update_or_query.message.reply_text(
                    text=page_text,
                    reply_markup=page_reply_markup
                )
            else:
                await update_or_query.message.reply_text(text=page_text)


@check_user_permission
async def handle_search_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理搜索结果翻页"""
    query = update.callback_query
    
    try:
        callback_data = json.loads(query.data)
        page = callback_data.get("page", 0)
        
        # 获取搜索结果
        search_results = context.user_data.get("search_results", [])
        
        if not search_results:
            await query.answer("❌ 未找到搜索结果，请重新搜索", show_alert=True)
            return
        
        await query.answer(f"📄 跳转到第 {page + 1} 页")
        await show_paged_results(query, context, search_results, page, 5)
        
    except json.JSONDecodeError:
        await query.answer("❌ 数据解析失败", show_alert=True)
    except Exception as e:
        logger.error(f"处理翻页失败: {e}")
        await query.answer("❌ 翻页失败，请重试", show_alert=True)