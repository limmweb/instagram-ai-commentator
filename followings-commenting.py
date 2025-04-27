import os
import time
import random
import json
import logging
import configparser
import requests
from datetime import datetime, timedelta
from instagrapi import Client
from instagrapi.exceptions import PleaseWaitFewMinutes
from openai import OpenAI
from filelock import FileLock
from pydantic_core import Url

# ----------------------------------------------------
# 1. ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ И ПАРАМЕТРЫ
# ----------------------------------------------------

SESSIONS_DIR = "Sessions"
LOGS_DIR = "logs"

# Telegram
TELEGRAM_BOT_TOKEN = "" #Enter your Telegram Bot ID to send notification on behalf of
TELEGRAM_CHAT_ID = #Enter Teelgram Chat ID where you want notifications

# OpenAI
OPENAI_API_KEY = "" #Enter your OpenAI Key

# Интервалы и лимиты
SLEEP_INTERVALS = [5, 10, 20, 60, 120, 300]  # При 429
RANDOM_DELAY_MIN_BETWEEN_USERS = 1
RANDOM_DELAY_MAX_BETWEEN_USERS = 2
RANDOM_DELAY_MIN_BETWEEN_COMMENTS = 180
RANDOM_DELAY_MAX_BETWEEN_COMMENTS = 300
RATE_LIMIT_SLEEP = 3 * 3600
POST_CUTOFF_HOURS = 24  # Возраст постов
SUBSCRIPTIONS_POSTS_AMOUNT = 3  # Кол-во постов для обработки
POST_TYPES_FOR_COMMENTING = [1, 2, 8]  # Типы постов (фото, видео, альбом)
THRESHOLD_LENGTH_FOR_COMMENTING = 30  # Мин. длина описания

# ----------------------------------------------------
# 2. ЛОГИРОВАНИЕ
# ----------------------------------------------------

os.makedirs(LOGS_DIR, exist_ok=True)
logging.basicConfig(
    filename=os.path.join(LOGS_DIR, 'comment_followings_log.txt'),
    level=logging.INFO,
    format='|'.join(['%(levelname)s', '%(asctime)s', '%(message)s']),
    datefmt='%Y-%m-%d %H:%M:%S'
)

error_logger = logging.getLogger('error_logger')
error_handler = logging.FileHandler(os.path.join(LOGS_DIR, 'comment_followings_log_error.txt'), encoding='utf-8')
error_handler.setFormatter(
    logging.Formatter('|'.join(['%(levelname)s', '%(asctime)s', '%(message)s']),
                      datefmt='%Y-%m-%d %H:%M:%S')
)
error_logger.addHandler(error_handler)
error_logger.setLevel(logging.ERROR)

# ----------------------------------------------------
# 3. ФУНКЦИЯ ОТПРАВКИ В TELEGRAM
# ----------------------------------------------------

def send_telegram_message(msg: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        r = requests.post(url, data=data, timeout=20)
        if r.status_code != 200:
            error_logger.error(f"[TELEGRAM] status={r.status_code}, text={r.text}")
        else:
            print("[TELEGRAM] Message sent successfully.")
    except Exception as e:
        error_logger.error(f"[TELEGRAM] {e}")

def log_error(msg: str):
    error_logger.error(msg)
    print("[ERROR]", msg)
    send_telegram_message(f"ERROR: {msg}")

# ----------------------------------------------------
# 4. УТИЛИТЫ ДЛЯ JSON, CONFIG
# ----------------------------------------------------

def ensure_json_file(path):
    if not os.path.exists(path):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump([], f, ensure_ascii=False, indent=2)

def read_json_list(path):
    ensure_json_file(path)
    with open(path, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []

def write_json_list(path, data_list):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data_list, f, ensure_ascii=False, indent=2)

def load_config(session_path):
    config_file = os.path.join(session_path, 'ig_login.ini')
    if not os.path.exists(config_file):
        raise FileNotFoundError(f"Config file not found: {config_file}")
    c = configparser.ConfigParser()
    with open(config_file, 'r', encoding='utf-8') as f:
        c.read_file(f)
    for section in ['Instagram', 'OpenAI', 'Session']:
        if section not in c:
            raise KeyError(f"Missing section [{section}] in config.")
    return c

def save_config(session_path, config):
    config_file = os.path.join(session_path, 'ig_login.ini')
    with open(config_file, 'w', encoding='utf-8') as f:
        config.write(f)

# ----------------------------------------------------
# 5. СОЗДАНИЕ / ВЫБОР СЕССИИ
# ----------------------------------------------------

def create_new_session():
    session_name = input("Введите имя новой сессии: ")
    session_path = os.path.join(SESSIONS_DIR, session_name)
    os.makedirs(session_path, exist_ok=True)
    ig_login = input("Instagram Login: ")
    ig_pass = input("Instagram Password: ")

    cl = Client()
    try:
        print("[INFO] Попытка входа (создание новой сессии).")
        cl.login(ig_login, ig_pass)
    except Exception as e:
        if "two-factor" in str(e).lower() or "2fa" in str(e).lower():
            print("[WARNING] 2FA. Введите код и нажмите Enter...")
            input()
            try:
                cl.login(ig_login, ig_pass)
            except Exception as e2:
                log_error(f"Session Creation Error (2FA): {e2}")
                return None
        else:
            log_error(f"Session Creation Error: {e}")
            return None

    profile = cl.user_info_by_username(ig_login)
    cfg = configparser.ConfigParser()
    cfg['Instagram'] = {
        'ig_login': ig_login,
        'ig_pass': ig_pass,
        'ig_id': str(profile.pk),
        'ig_username': profile.username,
        'ig_full_name': profile.full_name,
        'ig_profile_pic': str(profile.profile_pic_url),
        'ig_profile_description': profile.biography
    }
    cfg['OpenAI'] = {
        'openai_input_tokens_consumed': '0',
        'openai_output_tokens_consumed': '0',
        'openai_total_tokens_consumed': '0',
        'openai_tokens_cost': '0.0'
    }
    cfg['Comments'] = {'comments_done': ''}  # Добавляем секцию Comments
    cfg['Session'] = {'path': session_path}

    save_config(session_path, cfg)
    cl.dump_settings(os.path.join(session_path, 'session.json'))
    print(f"[SUCCESS] Сессия '{session_name}' создана.")
    return session_path

def select_session():
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    sessions = os.listdir(SESSIONS_DIR)
    if not sessions:
        return create_new_session()

    print("Существующие сессии:")
    for i, sess in enumerate(sessions, 1):
        print(f"{i}: {sess}")
    print("0: Создать новую сессию")
    choice = input("Выберите сессию: ")
    if choice == '0':
        return create_new_session()
    try:
        choice_num = int(choice)
        if 1 <= choice_num <= len(sessions):
            return os.path.join(SESSIONS_DIR, sessions[choice_num - 1])
    except:
        pass
    print("[ERROR] Неверный выбор.")
    return None

# ----------------------------------------------------
# 6. ПЕРЕСОЗДАНИЕ СЕССИИ
# ----------------------------------------------------

def remove_and_recreate_session(cl, session_path):
    while True:
        try:
            print("[INFO] Пытаемся logout перед удалением сессии...")
            cl.logout()
            print("[SUCCESS] Logout выполнен успешно.")
            break
        except Exception as e:
            err_str = f"[ERROR] logout failed: {e}\nНажмите Enter, чтобы повторить logout (или Ctrl+C для выхода)."
            print(err_str)
            send_telegram_message(err_str)
            input()

    session_file = os.path.join(session_path, 'session.json')
    if os.path.exists(session_file):
        os.remove(session_file)
        print("[INFO] Старый session.json удалён после logout.")

    cfg = load_config(session_path)
    ig_login = cfg['Instagram']['ig_login']
    ig_pass = cfg['Instagram']['ig_pass']

    cl.set_settings({})
    while True:
        try:
            print("[INFO] Логин после logout...")
            cl.login(ig_login, ig_pass)
            print("[SUCCESS] Перелогин прошёл успешно.")
            profile = cl.user_info_by_username(ig_login)
            cfg['Instagram']['ig_id'] = str(profile.pk)
            cfg['Instagram']['ig_username'] = profile.username
            cfg['Instagram']['ig_full_name'] = profile.full_name
            cfg['Instagram']['ig_profile_pic'] = str(profile.profile_pic_url)
            cfg['Instagram']['ig_profile_description'] = profile.biography

            cl.dump_settings(session_file)
            save_config(session_path, cfg)
            print("[SUCCESS] Сессия пересоздана полностью.")
            break
        except Exception as e:
            if "two-factor" in str(e).lower() or "2fa" in str(e).lower():
                print("[WARNING] 2FA. Введите код, Enter...")
                input()
                continue
            elif "challenge_required" in str(e).lower():
                print("[WARNING] Challenge required. Подтвердите в IG, Enter...")
                input()
                continue
            elif "429" in str(e):
                print("[ERROR] 429 при пересоздании, спим по SLEEP_INTERVALS")
                send_telegram_message("429 при пересоздании сессии, уходим в прогрессивный сон")
                for mins in SLEEP_INTERVALS:
                    print(f"Сон {mins} мин...")
                    time.sleep(mins * 60)
                continue
            else:
                err_str = f"[ERROR] Ошибка при пересоздании сессии: {e}\nНажмите Enter, чтобы повторить (или Ctrl+C)"
                print(err_str)
                send_telegram_message(err_str)
                input()

# ----------------------------------------------------
# 7. ИНИЦИАЛИЗАЦИЯ CLIENT
# ----------------------------------------------------

def init_instagram_client(session_path):
    cfg = load_config(session_path)
    ig_login = cfg['Instagram']['ig_login']
    ig_pass = cfg['Instagram']['ig_pass']
    cl = Client()

    session_file = os.path.join(session_path, 'session.json')
    need_login = True

    if os.path.exists(session_file):
        try:
            cl.load_settings(session_file)
            cl.login(ig_login, ig_pass)
            print(f"[SUCCESS] Авторизация через сохранённую сессию: {ig_login}")
            need_login = False
        except Exception as e:
            log_error(f"Ошибка автологина: {e}")

    while need_login:
        try:
            print("[INFO] Пробуем логин...")
            cl.login(ig_login, ig_pass)
            print(f"[SUCCESS] Вход: {ig_login}")
            break
        except Exception as e:
            if "two-factor" in str(e).lower() or "2fa" in str(e).lower():
                print("[WARNING] 2FA. Введите код, Enter...")
                input()
                continue
            elif "challenge_required" in str(e).lower():
                print("[WARNING] Challenge. Подтвердите в IG, Enter...")
                input()
                continue
            elif "429" in str(e):
                print("[ERROR] Лимит 429 при init. Спим...")
                for mins in SLEEP_INTERVALS:
                    print(f"Сон {mins} минут...")
                    time.sleep(mins * 60)
                continue
            else:
                log_error(f"Login error init: {e}")
                for mins in SLEEP_INTERVALS:
                    print(f"Сон {mins} мин...")
                    time.sleep(mins * 60)

    cl.dump_settings(session_file)
    return cl, cfg

# ----------------------------------------------------
# 8. ПОДСЧЁТ OPENAI
# ----------------------------------------------------

def add_openai_usage(config, input_tokens, output_tokens, cost):
    total_tokens = input_tokens + output_tokens
    old_in = int(config['OpenAI'].get('openai_input_tokens_consumed', 0))
    old_out = int(config['OpenAI'].get('openai_output_tokens_consumed', 0))
    old_total = int(config['OpenAI'].get('openai_total_tokens_consumed', 0))
    old_cost = float(config['OpenAI'].get('openai_tokens_cost', 0.0))

    config['OpenAI']['openai_input_tokens_consumed'] = str(old_in + input_tokens)
    config['OpenAI']['openai_output_tokens_consumed'] = str(old_out + output_tokens)
    config['OpenAI']['openai_total_tokens_consumed'] = str(old_total + total_tokens)
    config['OpenAI']['openai_tokens_cost'] = str(old_cost + cost)

# ----------------------------------------------------
# 9. OPENAI ФУНКЦИИ
# ----------------------------------------------------

def describe_image(ai_client, image_url):
    try:
        resp = ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Please very precisely describe this Instagram image, focusing on the style of depicting, guessing the mood, interpreting the meaning, and admitting any unique traits. If the image contains text, then scan it and include text unchanged into the description."},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
            max_tokens=300,
        )
        txt = resp.choices[0].message.content.strip()
        total = resp.usage.total_tokens
        cost = total * 0.00000035
        return txt, resp.usage.prompt_tokens, resp.usage.completion_tokens, cost
    except Exception as e:
        log_error(f"describe_image error: {e}")
        return "", 0, 0, 0.0

def generate_comment(ai_client, caption, image_desc):
    try:
        resp = ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "You are writing comments for Instagram posts of your followings. About you: You are witty art creator Alexander. You do create engaging, narrative-rich, sometimes with humor, comments that resonate emotionally and intellectually with the audience and, importantly, complementing autor of publication and his/her post in particular."
                },
                {
                    "role": "system",
                    "content": f"'Post caption': '{caption}', 'Post Image Description (AI-estimation)': '{image_desc}', 'Comment Language': 'Equal to Post Caption language, otherwise English', 'Comment Length': 30 - 120 symbols, 'Additional Rules': 'Use \"About you\" as reference for comment styling indirectly, do not reuse info About you directly in commentaries you produce; Rarely use emojis; Never use #hashtags.'"
                }
            ],
            max_tokens=120,
        )
        txt = resp.choices[0].message.content.strip()
        total = resp.usage.total_tokens
        cost = total * 0.00000035
        return txt, resp.usage.prompt_tokens, resp.usage.completion_tokens, cost
    except Exception as e:
        log_error(f"generate_comment error: {e}")
        return "", 0, 0, 0.0

# ----------------------------------------------------
# 10. ЛОГИКА КОММЕНТИРОВАНИЯ ПОДПИСОК
# ----------------------------------------------------

def default_serializer(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, Url):
        return str(obj)
    return str(obj)

def handle_rate_limit():
    send_telegram_message("Rate limit hit. Sleeping for 3 hours...")
    time.sleep(RATE_LIMIT_SLEEP)

def can_comment(post_data):
    post_type = post_data.get("media_type")
    description = post_data.get("caption_text", "")

    if post_type not in POST_TYPES_FOR_COMMENTING:
        print(f"[Комментирование Подписок] Тип поста {post_type} не разрешён для комментирования. Пропускаем.")
        return False

    if post_type != 1 and len(description) < THRESHOLD_LENGTH_FOR_COMMENTING:
        print(f"[Комментирование Подписок] Описание поста слишком короткое (менее {THRESHOLD_LENGTH_FOR_COMMENTING} символов), пропускаем.")
        return False

    return True

def random_delay(min_s, max_s):
    delay = random.uniform(min_s, max_s)
    print(f"Sleeping {delay:.1f}s...")
    time.sleep(delay)

def post_comment(cl, post_id, text):
    try:
        cl.media_comment(post_id, text)
        cl.media_like(post_id)
        return True
    except Exception as e:
        log_error(f"Failed to post comment: {e}")
        return False

def logic_comment_followings(cl, config, ai_client):
    session_path = config['Session']['path']
    username = config['Instagram']['ig_username']

    os.makedirs("sessions", exist_ok=True)
    os.makedirs("logs", exist_ok=True)

    followings_file = os.path.join(LOGS_DIR, f"{username}_followings.txt")

    while True:
        try:
            print("[Комментирование Подписок] Запрос user_following...")
            followings = cl.user_following(cl.user_id, amount=0)
            print(f"[Комментирование Подписок] Получено {len(followings)} подписок.")
            break
        except PleaseWaitFewMinutes:
            send_telegram_message("[Комментирование Подписок] PleaseWaitFewMinutes при user_following, спим 3 мин.")
            time.sleep(WAIT_FEW_MINUTES_SLEEP)
            continue
        except Exception as e:
            if "429" in str(e):
                print("[Комментирование Подписок] 429 при followings. Спим...")
                for mins in SLEEP_INTERVALS:
                    print(f"Сон {mins} минут...")
                    time.sleep(mins * 60)
                continue
            log_error(f"[Комментирование Подписок] Ошибка user_following: {e}, пересоздаём сессию.")
            remove_and_recreate_session(cl, session_path)
            continue

    follow_list = [u.username for u in followings.values()]

    if not os.path.exists(followings_file):
        with open(followings_file, 'w') as f:
            for user in follow_list:
                f.write(f"{user}\n")
    else:
        with open(followings_file, 'r') as f:
            existing = [x.strip() for x in f]
        new_users = [u for u in follow_list if u not in existing]
        final_list = new_users + existing
        with open(followings_file, 'w') as f:
            for u in final_list:
                f.write(f"{u}\n")

    with open(followings_file, 'r') as f:
        final_list = [x.strip() for x in f.readlines()]

    commented_log = "commented.txt"
    if not os.path.exists(commented_log):
        open(commented_log, 'w').close()

    idx = 0
    while idx < len(final_list):
        user = final_list[idx]
        try:
            print(f"[Комментирование Подписок] --- User={user}")
            while True:
                try:
                    user_id = cl.user_id_from_username(user)
                    posts = cl.user_medias(user_id, amount=SUBSCRIPTIONS_POSTS_AMOUNT)
                    print(f"[Комментирование Подписок] У пользователя {user} получено {len(posts)} постов.")
                    break
                except PleaseWaitFewMinutes:
                    send_telegram_message("PleaseWaitFewMinutes при user_medias. Спим 3 минуты.")
                    time.sleep(WAIT_FEW_MINUTES_SLEEP)
                    continue
                except Exception as e2:
                    if "429" in str(e2):
                        print("[Комментирование Подписок] 429 при user_medias. Спим...")
                        for mins in SLEEP_INTERVALS:
                            print(f"Сон {mins} мин...")
                            time.sleep(mins * 60)
                        continue
                    log_error(f"[Комментирование Подписок] Ошибка user_medias: {e2}, пересоздаём сессию.")
                    remove_and_recreate_session(cl, session_path)
                    continue

            now_ts = datetime.now().timestamp()
            processed_any_post = False

            for p in posts:
                p_json = json.loads(json.dumps(p.model_dump(), default=default_serializer, ensure_ascii=False))
                post_ts = datetime.fromisoformat(p_json["taken_at"]).timestamp()

                if (now_ts - post_ts) > (POST_CUTOFF_HOURS * 3600):
                    print(f"[Комментирование Подписок] Пост {p_json['id']} старше {POST_CUTOFF_HOURS}ч, пропускаем.")
                    continue

                if not can_comment(p_json):
                    print(f"[Комментирование Подписок] Пост {p_json['id']} пропущен по типу или длине описания.")
                    continue

                recognition_text = ""
                in1, out1, cost1 = 0, 0, 0.0

                if p_json["media_type"] == 1:
                    desc, in1, out1, cost1 = describe_image(ai_client, p_json["thumbnail_url"])
                    print(f"[Комментирование Подписок] describe_image => {desc[:60]}...")
                    if any(x in desc.lower() for x in ["i'm sorry", "i am sorry", "i can't", "i can not"]):
                        print("[Комментирование Подписки] OpenAI не смог описать изображение, пропускаем.")
                        continue
                    recognition_text = desc
                    add_openai_usage(config, in1, out1, cost1)
                    save_config(session_path, config)

                com, in2, out2, cost2 = generate_comment(ai_client, p_json["caption_text"], recognition_text)
                print(f"[Комментирование Подписок] => Comment: {com[:60]}...")
                add_openai_usage(config, in2, out2, cost2)
                save_config(session_path, config)

                with open(commented_log, 'a') as cfile:
                    cfile.write(f"{p_json['id']}\n")

                if post_comment(cl, p_json["id"], com):
                    print(f"[Комментирование Подписок] Успешно прокомментировали {p_json['id']}")

                    total_tokens = (in1 + out1) + (in2 + out2)
                    total_cost = cost1 + cost2
                    msg = (
                        f"Комментирование Подписок\n\n"
                        f"User: {user}\n\n"
                        f"Post: https://instagram.com/p/{p_json['code']}\n\n"
                        f"Caption: {p_json['caption_text']}\n\n"
                        f"Image desc: {recognition_text}\n\n"
                        f"-----------------------------------------\n\n"
                        f"Comment: {com}\n\n"
                        f"Tokens used: {total_tokens}, cost={total_cost:.8f}\n"
                    )
                    send_telegram_message(msg)

                    processed_any_post = True
                    random_delay(RANDOM_DELAY_MIN_BETWEEN_COMMENTS, RANDOM_DELAY_MAX_BETWEEN_COMMENTS)

            with open(followings_file, 'r') as f:
                lines = [x.strip() for x in f if x.strip() != user]
            lines.append(user)
            with open(followings_file, 'w') as f:
                for line_ in lines:
                    f.write(line_ + "\n")
            print(f"[Комментирование Подписок] Пользователь {user} перенесен в конец списка followings.")

        except Exception as e:
            err_str = f"[Комментирование Подписок] Error processing {user}: {e}"
            print(err_str)
            send_telegram_message(err_str)
            if "429" in str(e) or "rate limit" in str(e).lower():
                handle_rate_limit()
            else:
                with open(followings_file, 'r') as f:
                    lines = [x.strip() for x in f if x.strip() != user]
                lines.append(user)
                with open(followings_file, 'w') as f:
                    for line_ in lines:
                        f.write(line_ + "\n")

        idx += 1
        random_delay(RANDOM_DELAY_MIN_BETWEEN_USERS, RANDOM_DELAY_MAX_BETWEEN_USERS)

# ----------------------------------------------------
# 11. MAIN
# ----------------------------------------------------

def main():
    session_path = None
    while not session_path:
        session_path = select_session()
    if not session_path:
        print("[ERROR] Не удалось выбрать/создать сессию.")
        return

    cl, config = init_instagram_client(session_path)
    if not cl:
        print("[ERROR] Не удалось войти в Instagram.")
        return

    try:
        ai_client = OpenAI(api_key=OPENAI_API_KEY)
        print("[OPENAI] Инициализирован.")
    except Exception as e:
        log_error(f"OpenAI error: {e}")
        return

    try:
        logic_comment_followings(cl, config, ai_client)
    except KeyboardInterrupt:
        print("[INFO] Программа была остановлена пользователем.")
    finally:
        print("[INFO] Программа завершается.")

if __name__ == "__main__":
    main()
