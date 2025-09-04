import json
import logging
import hashlib
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler
from utils.api import call_danmaku_api
from utils.permission import check_user_permission

# 初始化日志
logger = logging.getLogger(__name__)
# 对话状态常量
EPISODES_PER_PAGE = 10  # 每页显示分集数量
INPUT_EPISODE_RANGE = 1  # 集数输入对话状态
CALLBACK_DATA_MAX_LEN = 64  # Telegram Bot API限制


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

    # 3. 按钮加载状态提示
    await query.answer("🔄 正在发起导入请求...", show_alert=False)

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
• 提示：可稍后用 /get_anime [作品ID] 查看详情
        """.strip())
    else:
        # 发送失败原因
        await query.message.reply_text(f"""
❌ 导入失败：{api_result['error']}
• 建议：若多次失败，可尝试重新搜索后导入
        """.strip())


@check_user_permission
async def handle_get_episode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    logger.info(f"📥 收到分集回调数据：{query.data}")
    await query.answer("处理中...", show_alert=False)

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

            # 临时更新按钮为加载状态（使用空回调避免长度问题）
            try:
                loading_keyboard = [[InlineKeyboardButton(text="⏳ 加载分集中...", callback_data="empty")]]
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(loading_keyboard))
            except BadRequest as e:
                logger.warning(f"⚠️ 编辑加载按钮失败：{str(e)}")

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

                fail_keyboard = [[InlineKeyboardButton(
                    text="🔄 重新获取分集",
                    callback_data=retry_callback
                )]]
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
                empty_keyboard = [[InlineKeyboardButton(
                    text="🔄 重新获取分集",
                    callback_data=retry_callback
                )]]
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
💡 支持输入格式：1-10 / 1,10 / 1,5-10

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
            
            # 集数输入按钮行
            input_callback = json.dumps({
                "a": "start_input_range",
                "d": data_id
            }, ensure_ascii=False)
            if len(input_callback) > CALLBACK_DATA_MAX_LEN:
                safe_id_len = 29
                input_callback = json.dumps({
                    "a": "start_input_range",
                    "d": data_id[:safe_id_len]
                }, ensure_ascii=False)
            buttons.append([InlineKeyboardButton(text="📝 输入集数区间", callback_data=input_callback)])
            
            # 立即导入按钮行
            import_callback = json.dumps({
                "action": "import_media",
                "result_index": result_index
            }, ensure_ascii=False)
            buttons.append([InlineKeyboardButton(text="🔗 立即导入全部", callback_data=import_callback)])
            
            logger.info(f"📤 发送分集列表消息，总集数: {len(full_episodes)}, 当前页: {current_page}/{total_pages}")
            await query.message.reply_text(
                text=full_message,
                reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
                parse_mode=None
            )
            logger.info(f"✅ 分集列表消息发送成功")

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
                f"📝 请输入需要导入的集数区间（当前共{total_episodes}集）：\n"
                f"示例：1-10 / 1,10 / 1,5-10",
                parse_mode=None
            )
            return INPUT_EPISODE_RANGE

        # 处理分页显示逻辑（仅在需要显示分页时执行）
        elif action in ["switch_episode_page", "get_episodes"]:
            logger.info(f"📋 进入分页显示逻辑，action: {action}, data_id: {data_id}")
            # 处理翻页动作：switch_episode_page
            if action == "switch_episode_page":
                logger.info(f"📄 处理翻页请求：切换到第{current_page}页")
            elif action == "get_episodes":
                logger.info(f"📋 处理获取分集请求，准备显示分集列表")

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
            
            # 集数输入按钮行
            input_callback = json.dumps({
                "a": "start_input_range",
                "d": data_id
            }, ensure_ascii=False)
            if len(input_callback) > CALLBACK_DATA_MAX_LEN:
                safe_id_len = 29
                input_callback = json.dumps({
                    "a": "start_input_range",
                    "d": data_id[:safe_id_len]
                }, ensure_ascii=False)
            buttons.append([InlineKeyboardButton(text="📝 输入集数区间", callback_data=input_callback)])
            
            # 立即导入全部按钮行（在所有页面都显示）
            # 需要获取原始result_index
            original_result_index = current_data.get("result_index", 0)
            import_callback = json.dumps({
                "action": "import_media",
                "result_index": original_result_index
            }, ensure_ascii=False)
            buttons.append([InlineKeyboardButton(text="🔗 立即导入全部", callback_data=import_callback)])
            
            full_message = f"""✅ 共找到 {total_episodes} 集有效分集 {page_info}
💡 支持输入格式：1-10 / 1,10 / 1,5-10

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

    except BadRequest as e:
        # 捕获Telegram按钮相关错误（如Button_data_invalid）
        logger.error(f"❌ 按钮回调错误：{str(e)}（当前回调长度：{len(query.data) if query.data else 0}）", exc_info=True)
        await query.answer("❌ 操作失败，请重新获取分集", show_alert=True)
        # 恢复基础按钮（使用最短回调）
        if "data_id" in locals():
            try:
                retry_callback = json.dumps({"action": "get_media_episode", "data_id": data_id[:6]}, ensure_ascii=False)
                error_keyboard = [[InlineKeyboardButton(
                    text="🔄 重新获取分集",
                    callback_data=retry_callback
                )]]
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(error_keyboard))
            except Exception:
                pass
    except Exception as e:
        logger.error(f"❌ 分集处理异常：{str(e)}", exc_info=True)
        await query.answer("❌ 处理失败，请重试", show_alert=True)
        if "data_id" in locals():
            try:
                retry_callback = json.dumps({"action": "get_media_episode", "data_id": data_id[:6]}, ensure_ascii=False)
                error_keyboard = [[InlineKeyboardButton(
                    text="🔄 重新获取分集",
                    callback_data=retry_callback
                )]]
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

    # 解析集数（逻辑不变，仅数据来源改为短ID映射）
    episode_index_map = {ep["episodeIndex"]: ep for ep in full_episodes}
    valid_episode_indices = set(episode_index_map.keys())
    range_segments = [seg.strip() for seg in user_input.split(",") if seg.strip()]

    if not range_segments:
        await update.message.reply_text("❌ 输入为空，请重新输入（示例：1-10 / 1,10）")
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
        msg += f"当前支持集数：1-{total_episodes}"
        await update.message.reply_text(msg)
        return INPUT_EPISODE_RANGE

    # 显示选中结果 + 准备导入
    sorted_indices = sorted(selected_indices)
    await update.message.reply_text(
        f"✅ 共选中 {len(sorted_indices)} 集：\n"
        f"选中集数：{', '.join(map(str, sorted_indices))}\n"
        f"💡 即将开始导入",
        parse_mode=None
    )

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
                f"• 提示：可稍后用 /get_anime [作品ID] 查看详情"
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