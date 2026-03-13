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

# アラート時刻設定（時, 分）
ALERT_TIMES = {
    "起床": (6,  0),   # 6:00  までに起床報告がない場合
    "出発": (7,  0),   # 7:00  までに出発報告がない場合
    "到着": (9, 50),   # 9:50  までに到着報告がない場合
}

# 何件メッセージが溜まったらパネルを再送信するか
PANEL_REFRESH_COUNT = 7

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
        "message_count": {}    # uid -> int（パネル再送信カウント用）
    }


def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_today_str() -> str:
    return datetime.date.today().isoformat()


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
    now      = datetime.datetime.now()
    date_str = now.strftime("%Y/%m/%d")
    time_str = now.strftime("%H:%M")

    embed = discord.Embed(
        title=f"{action_emoji.get(action, '')} {display_name}さんから！{action}報告を送信しました！",
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
    """毎分チェックして未報告アラートを送信"""
    await bot.wait_until_ready()

    # すでに送ったアラートを記録（同じ日に2回送らないため）
    alerted_today: dict[str, set] = {}  # uid -> {action, ...}

    while not bot.is_closed():
        now   = datetime.datetime.now()
        today = get_today_str()

        # 日付が変わったらリセット
        for uid in list(alerted_today.keys()):
            alerted_today[uid] = set()

        data = load_data()

        for uid, emp in data["employees"].items():
            if uid not in alerted_today:
                alerted_today[uid] = set()

            for action, (alert_h, alert_m) in ALERT_TIMES.items():
                # アラート時刻を過ぎているか
                alert_time = now.replace(hour=alert_h, minute=alert_m, second=0, microsecond=0)
                if now < alert_time:
                    continue  # まだ時間前

                # 今日すでにアラート送信済みか
                if action in alerted_today[uid]:
                    continue

                # 今日すでに報告済みか
                if has_reported(uid, action):
                    alerted_today[uid].add(action)
                    continue

                # アラート送信
                channel = None
                for guild in bot.guilds:
                    channel = guild.get_channel(emp["channel_id"])
                    if channel:
                        break

                if channel:
                    action_emoji = {"起床": "🌅", "出発": "🚶", "到着": "🏢"}
                    embed = discord.Embed(
                        title=f"⏰ {action}報告のお願い",
                        description=f"{emp['display_name']}さん、{action}報告お願いします！",
                        color=discord.Color.yellow()
                    )
                    embed.set_footer(text=f"アラート時刻: {now.strftime('%H:%M')}")
                    await channel.send(f"<@{uid}>", embed=embed)
                    print(f"[アラート送信] {emp['display_name']} / {action}")

                alerted_today[uid].add(action)

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

    # アラート時刻も表示
    alert_lines = [f"・{action}：{h:02d}:{m:02d} までに未報告でアラート" for action, (h, m) in ALERT_TIMES.items()]
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
            embed.set_footer(text=f"管理者より {datetime.datetime.now().strftime('%Y/%m/%d %H:%M')}")
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
