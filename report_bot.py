"""
Discord 従業員報告Bot
====================
・/add_employee でプライベートチャンネルを自動作成＆パネル設置
・チャンネル名は自由に指定可能
・ボタンで起床/出発/到着 + 体調を選択
・報告は管理者のDMに送信、管理者はDMから返信可能
・未報告アラート機能：
    6:00  までに起床報告がない → 個人チャンネルに「起床報告お願いします！」
    7:00  までに出発報告がない → 個人チャンネルに「出発報告お願いします！」
    9:50  までに到着報告がない → 個人チャンネルに「到着報告お願いします！」
・設定はdata.jsonに自動保存（再起動後も維持）

必要パッケージ:
    pip install discord.py

起動:
    python report_bot.py

====================
スラッシュコマンド一覧（管理者権限が必要）
====================
  /add_employee @ユーザー 表示名 チャンネル名
      例: /add_employee @田中太郎 田中 tanaka-report

  /remove_employee @ユーザー  → 従業員を削除
  /add_admin @ユーザー        → 管理者を追加
  /remove_admin @ユーザー     → 管理者を削除
  /list                       → 登録一覧を表示
  /setup                      → パネルを再設置

====================
管理者からの返信方法
====================
  管理者がBotのDMに届いた報告を見て、そのままDMで返信すると
  対象の従業員チャンネルに自動転送されます。
"""

import discord
from discord.ext import commands
from discord import app_commands
import datetime
import json
import os
import asyncio

# ==========================================
# 設定（BOT_TOKENだけ変更してください）
# ==========================================

import os
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATA_FILE = "data.json"

# デフォルトのアラート時刻（コマンドで変更可能・日本時間）
DEFAULT_ALERT_TIMES = {
    "起床": (8, 0),
    "出発": (8, 0),
    "到着": (8, 0),
}

JST = datetime.timezone(datetime.timedelta(hours=9))  # 日本時間
ALERT_MAX_COUNT = 2  # アラートの最大送信回数

# ==========================================
# データ管理
# ==========================================

def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "employees":     {},   # uid -> {display_name, channel_id, channel_name}
        "admins":        [],   # [uid, ...]
        "last_report":   {},   # admin_uid -> reporter_uid（返信転送用）
        "today_reports": {},   # uid -> {起床: bool, 出発: bool, 到着: bool, date: str}
        "message_count": {},   # uid -> int（パネル再送信カウント用）
        "alert_times": {}      # action -> [h, m]
    }


def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_today_str() -> str:
    return datetime.datetime.now(JST).date().isoformat()


def mark_reported(uid: str, action: str):
    """報告済みフラグを記録"""
    data = load_data()
    today = get_today_str()

    if "today_reports" not in data:
        data["today_reports"] = {}

    if uid not in data["today_reports"] or data["today_reports"][uid].get("date") != today:
        # 日付が変わっていたらリセット
        data["today_reports"][uid] = {"date": today, "起床": False, "出発": False, "到着": False}

    data["today_reports"][uid][action] = True
    save_data(data)


def has_reported(uid: str, action: str) -> bool:
    """今日その報告が済んでいるか"""
    data = load_data()
    today = get_today_str()
    record = data.get("today_reports", {}).get(uid)
    if not record or record.get("date") != today:
        return False
    return record.get(action, False)


# ==========================================
# Bot本体
# ==========================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree




async def send_dm_to_admins(embed: discord.Embed, reporter_uid: str):
    """管理者全員にDM送信"""
    data = load_data()
    for admin_id in data["admins"]:
        try:
            admin_user = await bot.fetch_user(int(admin_id))
            dm = await admin_user.create_dm()
            await dm.send(embed=embed)
            data["last_report"][admin_id] = reporter_uid
        except discord.Forbidden:
            print(f"[警告] 管理者(ID:{admin_id}) へのDMが拒否されました")
        except Exception as e:
            print(f"[エラー] DM送信失敗 (ID:{admin_id}): {e}")
    save_data(data)


def build_report_embed(display_name: str, action: str, condition: str) -> discord.Embed:
    action_emoji    = {"起床": "🌅", "出発": "🚶", "到着": "🏢"}
    condition_emoji = {"いい": "😊", "まあまあ": "😐", "わるい": "😔"}
    now      = datetime.datetime.now(JST)
    date_str = now.strftime("%Y/%m/%d")
    time_str = now.strftime("%H:%M")

    embed = discord.Embed(
        title=f"{action_emoji.get(action, '')} {display_name}さんが！{action}報告を送信しました！",
        color=(
            discord.Color.green()  if condition == "いい"    else
            discord.Color.orange() if condition == "まあまあ" else
            discord.Color.red()
        ),
    )
    embed.add_field(name="👤 従業員",   value=display_name,                                        inline=True)
    embed.add_field(name="📋 報告種別", value=action,                                              inline=True)
    embed.add_field(name="🏥 体調",     value=f"{condition_emoji.get(condition, '')} {condition}", inline=True)
    embed.set_footer(text=f"📅 {date_str}　⏰ {time_str}")
    return embed


# ==========================================
# 未報告アラートループ
# ==========================================

async def alert_loop():
    """
    毎分チェックして未報告アラートを送信
    アラート送信済み判定はメモリ上の sent_today で管理。
    sent_today = { uid: { action: 送信回数 } }
    日付をまたいだらリセット。
    「アラート時刻を過ぎているかつ未報告」の場合のみ送信し、
    Bot起動時にすでにアラート時刻を過ぎていても today_reports に
    報告済みか sent_today に記録があればスキップする。
    """
    await bot.wait_until_ready()

    # メモリ上で管理（Railwayファイルリセット問題を回避）
    sent_today: dict[str, dict[str, int]] = {}  # uid -> {action -> 送信回数}
    last_date  = datetime.datetime.now(JST).date().isoformat()

    # 起動時点で「アラート時刻をすでに過ぎているもの」はスキップ対象として記録
    # → 起動直後の誤送信を防ぐ
    now_boot = datetime.datetime.now(JST)
    data_boot = load_data()
    for uid in data_boot.get("employees", {}):
        sent_today[uid] = {}
        for action, default in DEFAULT_ALERT_TIMES.items():
            saved = data_boot.get("alert_times", {}).get(action)
            alert_h, alert_m = saved if saved else default
            alert_time = now_boot.replace(hour=alert_h, minute=alert_m, second=0, microsecond=0)
            # 起動時点でアラート時刻を過ぎていたら送信済みとしてマーク
            if now_boot >= alert_time:
                sent_today[uid][action] = ALERT_MAX_COUNT
                print(f"[起動スキップ] {data_boot['employees'][uid]['display_name']} / {action} （起動時点で時刻超過）")

    while not bot.is_closed():
        now   = datetime.datetime.now(JST)
        today = now.date().isoformat()

        # 日付が変わったらリセット
        if today != last_date:
            sent_today  = {}
            last_date   = today

        data = load_data()

        # アラート時刻をdataから取得（なければデフォルト）
        alert_times = {}
        for action, default in DEFAULT_ALERT_TIMES.items():
            saved = data.get("alert_times", {}).get(action)
            alert_times[action] = tuple(saved) if saved else default

        for uid, emp in data["employees"].items():
            if uid not in sent_today:
                # 新しく検出したuidも起動時と同様にスキップ処理
                sent_today[uid] = {}
                for action, default in DEFAULT_ALERT_TIMES.items():
                    saved = data.get("alert_times", {}).get(action)
                    ah, am = saved if saved else default
                    at = now.replace(hour=ah, minute=am, second=0, microsecond=0)
                    if now >= at:
                        sent_today[uid][action] = ALERT_MAX_COUNT
                        print(f"[新規スキップ] {emp['display_name']} / {action}")

            for action, (alert_h, alert_m) in alert_times.items():
                count = sent_today[uid].get(action, 0)

                # 最大送信回数に達していたらスキップ
                if count >= ALERT_MAX_COUNT:
                    continue

                # 1回目：アラート時刻を過ぎているか（JST）
                # 2回目：1回目から15分後
                alert_time = now.replace(hour=alert_h, minute=alert_m, second=0, microsecond=0)
                second_alert_time = alert_time + datetime.timedelta(minutes=15)

                if count == 0 and now < alert_time:
                    continue
                if count == 1 and now < second_alert_time:
                    continue

                # 今日すでに報告済みか
                if has_reported(uid, action):
                    sent_today[uid][action] = ALERT_MAX_COUNT
                    continue

                # アラート送信
                channel = None
                for guild in bot.guilds:
                    channel = guild.get_channel(emp["channel_id"])
                    if channel:
                        break

                if channel:
                    embed = discord.Embed(
                        title=f"⏰ {emp['display_name']}さん、{action}報告お願いします！",
                        description=f"{emp['display_name']}さん、{action}報告お願いします！",
                        color=discord.Color.yellow()
                    )
                    embed.set_footer(text=f"アラート時刻: {now.strftime('%H:%M')} JST")
                    await channel.send(f"<@{uid}>", embed=embed)
                    print(f"[アラート送信] {emp['display_name']} / {action} ({count+1}回目)")

                sent_today[uid][action] = count + 1

        await asyncio.sleep(60)  # 1分ごとにチェック


# ==========================================
# 体調選択View
# ==========================================

class ConditionView(discord.ui.View):
    def __init__(self, display_name: str, action: str, reporter_uid: int):
        super().__init__(timeout=60)
        self.display_name = display_name
        self.action       = action
        self.reporter_uid = reporter_uid

    async def send_final_report(self, interaction: discord.Interaction, condition: str):
        embed = build_report_embed(self.display_name, self.action, condition)
        await interaction.response.edit_message(content=f"✅ {self.action}報告を送信しました！", view=None)
        await interaction.channel.send(embed=embed)
        mark_reported(str(self.reporter_uid), self.action)
        await send_dm_to_admins(embed, str(self.reporter_uid))


        # 報告するたびに新しいパネルを送信
        panel_embed = discord.Embed(
            title=f"📋 {self.display_name}さんの報告パネル",
            description="下のボタンで報告してください\n報告内容は管理者にDMで届きます",
            color=discord.Color.blurple()
        )
        await interaction.channel.send(embed=panel_embed, view=StatusView(
            display_name=self.display_name,
            employee_uid=self.reporter_uid
        ))

    @discord.ui.button(label="😊 いい",    style=discord.ButtonStyle.success)
    async def good(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.send_final_report(interaction, "いい")

    @discord.ui.button(label="😐 まあまあ", style=discord.ButtonStyle.primary)
    async def so_so(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.send_final_report(interaction, "まあまあ")

    @discord.ui.button(label="😔 わるい",  style=discord.ButtonStyle.danger)
    async def bad(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.send_final_report(interaction, "わるい")


# ==========================================
# 報告パネルView（常時表示）
# ==========================================

class StatusView(discord.ui.View):
    def __init__(self, display_name: str, employee_uid: int):
        super().__init__(timeout=None)
        self.display_name = display_name
        self.employee_uid = employee_uid
        # ユーザーIDを含む一意なcustom_idで重複を防ぐ
        self.add_item(ReportButton("🌅 起床", discord.ButtonStyle.primary,   f"wakeup_{employee_uid}", "起床", display_name, employee_uid))
        self.add_item(ReportButton("🚶 出発", discord.ButtonStyle.success,   f"depart_{employee_uid}", "出発", display_name, employee_uid))
        self.add_item(ReportButton("🏢 到着", discord.ButtonStyle.secondary, f"arrive_{employee_uid}", "到着", display_name, employee_uid))
        self.add_item(NippoButton(f"nippo_{employee_uid}", display_name, employee_uid))


class ReportButton(discord.ui.Button):
    def __init__(self, label: str, style: discord.ButtonStyle, custom_id: str, action: str, display_name: str, employee_uid: int):
        super().__init__(label=label, style=style, custom_id=custom_id)
        self.action       = action
        self.display_name = display_name
        self.employee_uid = employee_uid

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.employee_uid:
            await interaction.response.send_message("❌ このボタンはあなた専用ではありません", ephemeral=True)
            return
        await interaction.response.send_message(
            "体調を選んでください 👇",
            view=ConditionView(self.display_name, self.action, self.employee_uid),
            ephemeral=True
        )



class NippoButton(discord.ui.Button):
    def __init__(self, custom_id: str, display_name: str, employee_uid: int):
        super().__init__(label="📝 報告", style=discord.ButtonStyle.danger, custom_id=custom_id)
        self.display_name = display_name
        self.employee_uid = employee_uid

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.employee_uid:
            await interaction.response.send_message("❌ このボタンはあなた専用ではありません", ephemeral=True)
            return
        await interaction.response.send_modal(NippoModal(self.display_name, self.employee_uid))


# ==========================================
# 振り返りModal（1/2）
# ==========================================



# ==========================================
# 日報Modal（2/2）
# ==========================================

class NippoModal(discord.ui.Modal, title="📊 日報入力（2/2）"):
    def __init__(self, display_name: str, employee_uid: int, furikaeri: dict):
        super().__init__()
        self.display_name = display_name
        self.employee_uid = employee_uid
        self.furikaeri    = furikaeri

    date_goals = discord.ui.TextInput(
        label="日付 / 月間目標成約 / 月間目標スイング成約",
        style=discord.TextStyle.short,
        placeholder="例：3/13 / 10 / 5",
        required=True
    )
    daily = discord.ui.TextInput(
        label="当日着座数 / 当日成約 / 当日スイング成約",
        style=discord.TextStyle.short,
        placeholder="例：8 / 2 / 1",
        required=True
    )
    monthly = discord.ui.TextInput(
        label="月間累計成約 / 月間累計スイング / 残成約 / 残スイング",
        style=discord.TextStyle.short,
        placeholder="例：6 / 3 / 4 / 2",
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        now      = datetime.datetime.now(JST)
        date_str = now.strftime("%Y/%m/%d")
        time_str = now.strftime("%H:%M")

        # チャンネルに投稿するembed
        embed = discord.Embed(
            title=f"📝 {self.display_name}さんの日報",
            color=discord.Color.blurple()
        )
        embed.add_field(name="＝＝ 振り返り ＝＝", value="​", inline=False)
        for k, v in self.furikaeri.items():
            embed.add_field(name=k, value=v, inline=False)

        embed.add_field(name="＝＝ 日報 ＝＝", value="​", inline=False)
        embed.add_field(name="日付 / 月間目標成約 / 月間目標スイング",      value=self.date_goals.value, inline=False)
        embed.add_field(name="当日着座数 / 当日成約 / 当日スイング",         value=self.daily.value,      inline=False)
        embed.add_field(name="月間累計成約 / 累計スイング / 残成約 / 残スイング", value=self.monthly.value, inline=False)
        embed.set_footer(text=f"📅 {date_str}　⏰ {time_str}")

        await interaction.response.send_message("✅ 日報を送信しました！", ephemeral=True)
        await interaction.channel.send(embed=embed)
        await send_dm_to_admins(embed, str(self.employee_uid))



# ==========================================
# スラッシュコマンド
# ==========================================

@tree.command(name="従業員追加", description="従業員を追加してプライベートチャンネルを自動作成します")
@app_commands.describe(
    member       = "追加する従業員（@メンション）",
    display_name = "報告に表示する名前（例: 田中）",
    channel_name = "作成するチャンネル名（例: tanaka-report）"
)
@app_commands.checks.has_permissions(administrator=True)
async def add_employee(
    interaction: discord.Interaction,
    member: discord.Member,
    display_name: str,
    channel_name: str
):
    await interaction.response.defer(ephemeral=True)
    data = load_data()
    uid  = str(member.id)

    if uid in data["employees"]:
        await interaction.followup.send(f"⚠️ {member.mention} はすでに登録されています")
        return

    guild = interaction.guild
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me:           discord.PermissionOverwrite(view_channel=True, send_messages=True),
        member:             discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    for admin_id in data["admins"]:
        try:
            admin_member = guild.get_member(int(admin_id))
            if admin_member:
                overwrites[admin_member] = discord.PermissionOverwrite(view_channel=True, send_messages=False)
        except Exception:
            pass

    channel = await guild.create_text_channel(
        name=channel_name,
        overwrites=overwrites,
        reason=f"{display_name}さんの報告チャンネル（自動作成）"
    )

    embed = discord.Embed(
        title=f"📋 {display_name}さんの報告パネル",
        description="**毎日の報告はここから！**\n━━━━━━━━━━━━━━━━━━\n🌅 **起床** → 起きたら押す\n🚶 **出発** → 家を出たら押す\n🏢 **到着** → 職場に着いたら押す\n━━━━━━━━━━━━━━━━━━\n報告後に体調を選んでください\n📩 報告内容は管理者にDMで届きます",
        color=discord.Color.blurple()
    )
    await channel.send(content="# ⬇️ タップして報告 ⬇️", embed=embed, view=StatusView(display_name=display_name, employee_uid=member.id))

    data["employees"][uid] = {
        "display_name": display_name,
        "channel_id":   channel.id,
        "channel_name": channel_name
    }
    save_data(data)
    bot.add_view(StatusView(display_name=display_name, employee_uid=member.id))

    await interaction.followup.send(
        f"✅ {display_name}さん（{member.mention}）を登録しました\n"
        f"📁 プライベートチャンネル {channel.mention} を作成し、パネルを設置しました"
    )


@tree.command(name="従業員削除", description="従業員を削除します（チャンネルは残ります）")
@app_commands.describe(member="削除する従業員（@メンション）")
@app_commands.checks.has_permissions(administrator=True)
async def remove_employee(interaction: discord.Interaction, member: discord.Member):
    data = load_data()
    uid  = str(member.id)
    if uid not in data["employees"]:
        await interaction.response.send_message(f"⚠️ {member.mention} は登録されていません", ephemeral=True)
        return
    name = data["employees"][uid]["display_name"]
    del data["employees"][uid]
    save_data(data)
    await interaction.response.send_message(f"🗑️ {name}さん（{member.mention}）を削除しました", ephemeral=True)


@tree.command(name="管理者追加", description="管理者（DM通知先）を追加します")
@app_commands.describe(member="管理者にするユーザー（@メンション）")
@app_commands.checks.has_permissions(administrator=True)
async def add_admin(interaction: discord.Interaction, member: discord.Member):
    data = load_data()
    uid  = str(member.id)
    if uid in data["admins"]:
        await interaction.response.send_message(f"⚠️ {member.mention} はすでに管理者です", ephemeral=True)
        return
    data["admins"].append(uid)
    save_data(data)
    await interaction.response.send_message(f"✅ {member.mention} を管理者（DM通知先）に追加しました", ephemeral=True)


@tree.command(name="管理者削除", description="管理者を削除します")
@app_commands.describe(member="削除する管理者（@メンション）")
@app_commands.checks.has_permissions(administrator=True)
async def remove_admin(interaction: discord.Interaction, member: discord.Member):
    data = load_data()
    uid  = str(member.id)
    if uid not in data["admins"]:
        await interaction.response.send_message(f"⚠️ {member.mention} は管理者ではありません", ephemeral=True)
        return
    data["admins"].remove(uid)
    save_data(data)
    await interaction.response.send_message(f"🗑️ {member.mention} を管理者から削除しました", ephemeral=True)


@tree.command(name="一覧", description="従業員・管理者の登録一覧を表示します")
@app_commands.checks.has_permissions(administrator=True)
async def show_list(interaction: discord.Interaction):
    data  = load_data()
    embed = discord.Embed(title="📋 登録一覧", color=discord.Color.blurple())

    if data["employees"]:
        lines = [f"・{v['display_name']}（<@{uid}>）→ `#{v['channel_name']}`" for uid, v in data["employees"].items()]
        embed.add_field(name="👤 従業員", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="👤 従業員", value="未登録", inline=False)

    if data["admins"]:
        embed.add_field(name="🔑 管理者", value="\n".join([f"・<@{uid}>" for uid in data["admins"]]), inline=False)
    else:
        embed.add_field(name="🔑 管理者", value="未登録", inline=False)

    # アラート時刻をdataから取得して表示
    alert_lines = []
    for action, default in DEFAULT_ALERT_TIMES.items():
        saved = data.get("alert_times", {}).get(action)
        h, m = saved if saved else default
        label = "（カスタム）" if saved else "（デフォルト）"
        alert_lines.append(f"・{action}：{h:02d}:{m:02d} {label}")
    embed.add_field(name="⏰ アラート設定", value="\n".join(alert_lines), inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="パネル再設置", description="全従業員の報告パネルを再設置します")
@app_commands.checks.has_permissions(administrator=True)
async def setup_all(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    data = load_data()
    if not data["employees"]:
        await interaction.followup.send("⚠️ 従業員が登録されていません。")
        return
    for uid, v in data["employees"].items():
        channel = interaction.guild.get_channel(v["channel_id"])
        if channel is None:
            await interaction.followup.send(f"⚠️ `#{v['channel_name']}` チャンネルが見つかりません")
            continue
        embed = discord.Embed(
            title=f"📋 {v['display_name']}さんの報告パネル",
            description="**毎日の報告はここから！**\n━━━━━━━━━━━━━━━━━━\n🌅 **起床** → 起きたら押す\n🚶 **出発** → 家を出たら押す\n🏢 **到着** → 職場に着いたら押す\n━━━━━━━━━━━━━━━━━━\n報告後に体調を選んでください\n📩 報告内容は管理者にDMで届きます",
            color=discord.Color.blurple()
        )
        await channel.send(content="# ⬇️ タップして報告 ⬇️", embed=embed, view=StatusView(display_name=v["display_name"], employee_uid=int(uid)))
        await interaction.followup.send(f"✅ `#{v['channel_name']}` にパネルを再設置しました")
    await interaction.followup.send("🎉 セットアップ完了！")


# ==========================================
# パブリックチャンネル作成
# ==========================================

@tree.command(name="チャンネル作成", description="全員が見えるパブリックチャンネルを作成します")
@app_commands.describe(
    channel_name = "作成するチャンネル名（例: soudan-madoguchi）",
    description  = "チャンネルの説明（任意）"
)
@app_commands.checks.has_permissions(administrator=True)
async def create_public_channel(
    interaction: discord.Interaction,
    channel_name: str,
    description: str = ""
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    # 全員が見える・書き込めるパブリック設定
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        guild.me:           discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }

    channel = await guild.create_text_channel(
        name=channel_name,
        overwrites=overwrites,
        topic=description if description else None,
        reason="パブリックチャンネル（自動作成）"
    )

    embed = discord.Embed(
        title=f"📢 #{channel_name}",
        description=description if description else "このチャンネルはサーバー全員が参加できます。",
        color=discord.Color.green()
    )
    await channel.send(embed=embed)
    await interaction.followup.send(f"✅ パブリックチャンネル {channel.mention} を作成しました！\nサーバーに参加した全員が即座に見えます。")


@tree.command(name="チャンネル削除", description="パブリックチャンネルを削除します")
@app_commands.describe(channel="削除するチャンネル")
@app_commands.checks.has_permissions(administrator=True)
async def delete_public_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    name = channel.name
    await channel.delete(reason="管理者によるチャンネル削除")
    await interaction.response.send_message(f"🗑️ `#{name}` を削除しました", ephemeral=True)



@tree.command(name="アラート設定", description="アラート時刻を設定します（日本時間）")
@app_commands.describe(
    action = "設定する報告種別（起床 / 出発 / 到着）",
    hour   = "時（0〜23）",
    minute = "分（0〜59）"
)
@app_commands.checks.has_permissions(administrator=True)
async def set_alert(interaction: discord.Interaction, action: str, hour: int, minute: int):
    if action not in ["起床", "出発", "到着"]:
        await interaction.response.send_message("⚠️ 報告種別は「起床」「出発」「到着」のいずれかを入力してください", ephemeral=True)
        return
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        await interaction.response.send_message("⚠️ 時刻が正しくありません（時: 0〜23、分: 0〜59）", ephemeral=True)
        return

    data = load_data()
    if "alert_times" not in data:
        data["alert_times"] = {}
    data["alert_times"][action] = [hour, minute]
    save_data(data)

    await interaction.response.send_message(
        f"✅ {action}のアラート時刻を {hour:02d}:{minute:02d} に設定しました（日本時間）\nアラートは最大{ALERT_MAX_COUNT}回送信されます",
        ephemeral=True
    )



@tree.command(name="アラート削除", description="設定したアラート時刻をデフォルトに戻します")
@app_commands.describe(action="削除する報告種別（起床 / 出発 / 到着）")
@app_commands.checks.has_permissions(administrator=True)
async def delete_alert(interaction: discord.Interaction, action: str):
    if action not in ["起床", "出発", "到着"]:
        await interaction.response.send_message("⚠️ 報告種別は「起床」「出発」「到着」のいずれかを入力してください", ephemeral=True)
        return

    data = load_data()
    if "alert_times" not in data or action not in data.get("alert_times", {}):
        await interaction.response.send_message(f"⚠️ {action}のアラート設定はありません", ephemeral=True)
        return

    del data["alert_times"][action]
    save_data(data)

    h, m = DEFAULT_ALERT_TIMES[action]
    await interaction.response.send_message(
        f"🗑️ {action}のアラート設定を削除しました\nデフォルト時刻（{h:02d}:{m:02d}）に戻ります",
        ephemeral=True
    )


# ==========================================
# 管理者DMからの返信を従業員チャンネルに転送
# ==========================================

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if not isinstance(message.channel, discord.DMChannel):
        await bot.process_commands(message)
        return

    data     = load_data()
    admin_id = str(message.author.id)
    if admin_id not in data["admins"]:
        return

    reporter_uid = data["last_report"].get(admin_id)
    if not reporter_uid:
        await message.channel.send("⚠️ 返信先の従業員が見つかりません。")
        return

    employee = data["employees"].get(reporter_uid)
    if not employee:
        await message.channel.send("⚠️ 該当の従業員が登録されていません。")
        return

    for guild in bot.guilds:
        channel = guild.get_channel(employee["channel_id"])
        if channel:
            embed = discord.Embed(
                title="📩 管理者からのメッセージ",
                description=message.content,
                color=discord.Color.gold()
            )
            embed.set_footer(text=f"管理者より {datetime.datetime.now(JST).strftime('%Y/%m/%d %H:%M')}")
            await channel.send(f"<@{reporter_uid}>", embed=embed)
            await message.channel.send(f"✅ {employee['display_name']}さんのチャンネルに送信しました")
            return

    await message.channel.send("⚠️ 送信先チャンネルが見つかりませんでした。")


# ==========================================
# 起動
# ==========================================

@bot.event
async def on_ready():
    print(f"✅ Bot起動: {bot.user}")
    data = load_data()
    await tree.sync()
    print("✅ スラッシュコマンド同期完了")

    for uid, v in data["employees"].items():
        bot.add_view(StatusView(display_name=v["display_name"], employee_uid=int(uid)))

    # アラートループ開始
    bot.loop.create_task(alert_loop())
    print("✅ アラートループ開始")
    print(f"登録済み従業員: {len(data['employees'])}名 / 管理者: {len(data['admins'])}名")


bot.run(BOT_TOKEN)
