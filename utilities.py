import logging
import os
import sys
import time
import math
from datetime import datetime, timedelta, timezone

import hjson
import pandas as pd
import psutil
import redis
import shortuuid
from dotenv import load_dotenv
from faker import Faker
from sqlalchemy import create_engine

load_dotenv("dev.env")

Faker.seed(int(os.getenv("seed")))
fake = Faker()

num_uuid = shortuuid.ShortUUID()
num_uuid.set_alphabet("0123456789")
back_range = 61

with open("config.hjson") as f:
    config = hjson.load(f)
config["command_channels"] = set(config["command_channels"])

role_settings = config["study_roles"]
role_name_to_begin_hours = {role_name: float(role_info['hours'].split("-")[0]) for role_name, role_info in
                            role_settings.items()}
role_names = list(role_settings.keys())


def get_rank_categories():
    rank_categories = {
        "daily": f"{get_day_start()}_daily",
        "weekly": f"{get_week_start()}_weekly",
        "monthly": f"{get_month()}_monthly",
        "all_time": "all_time"
    }

    return rank_categories


def get_logger(job_name, filename):
    logger = logging.getLogger(job_name)
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(filename=filename, encoding='utf-8', mode='a')
    handler.setFormatter(logging.Formatter('%(message)s:%(levelname)s:%(name)s:%(process)d'))
    logger.addHandler(handler)

    return logger


def get_guildID():
    guildID_key_name = ("test_" if os.getenv("mode") == "test" else "") + "guildID"
    guildID = int(os.getenv(guildID_key_name))
    return guildID


def recreate_db(Base):
    redis_client = get_redis_client()
    redis_client.flushall()
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def get_engine(echo=False):
    # if os.getenv("mode") != "test":
    #     echo = False

    return create_engine(
        f'mysql+pymysql://{os.getenv("sql_user")}:{os.getenv("sql_password")}@{os.getenv("sql_host")}/{os.getenv("sql_database")}',
        echo=echo)


def get_time():
    now = datetime.utcnow()
    return now


def get_num_days_this_month():
    return datetime.utcnow().day


def get_day_start():
    dt = datetime.combine(datetime.utcnow().date(), datetime.min.time())
    offset = timedelta(hours=config["business"]["update_time"])

    if datetime.utcnow() < dt + offset:
        offset -= timedelta(days=1)

    return dt + offset


def get_tomorrow_start():
    return get_day_start() + timedelta(days=1)


def get_week_start():
    return get_day_start() - timedelta(days=get_day_start().weekday() % 7)


def get_month_start():
    given_date = get_day_start()
    first_day_of_month = given_date - timedelta(days=int(given_date.strftime("%d")) - 1)
    return first_day_of_month


def get_earliest_start():
    return datetime.utcnow() - timedelta(days=back_range)


def get_month():
    return datetime.utcnow().strftime("%B")


def timedelta_to_hours(td):
    return td.total_seconds() / 3600


def round_num(num, ndigits=2):
    if os.getenv("mode") == "test":
        ndigits = int(os.getenv("test_display_num_decimal"))

    return round(num, ndigits=ndigits)


def calc_total_time(data):
    if not data:
        return 0

    total_time = timedelta(0)
    start_idx = 0
    end_idx = len(data) - 1

    if data[0]["category"] == "end channel":
        total_time += data[0]["creation_time"] - get_month_start()
        start_idx = 1

    if data[-1]["category"] == "start channel":
        total_time += get_time() - data[-1]["creation_time"]
        end_idx -= 1

    for idx in range(start_idx, end_idx + 1, 2):
        total_time += data[idx + 1]["creation_time"] - data[idx]["creation_time"]

    total_time = timedelta_to_hours(total_time)
    return total_time


def generate_random_number(size=1, length=18):
    res = [fake.random_number(digits=length, fix_len=True) for _ in range(size)]
    return res


def generate_discord_user_id(size=1, length=18):
    res = []

    if size >= 2:
        res += [int(os.getenv("tester_human_discord_user_id")), int(os.getenv("tester_bot_token_discord_user_id"))]
        size -= 2

    res += generate_random_number(size, length)

    return res


def generate_datetime(size=1, start_date=f'-{back_range}d'):
    return sorted([fake.past_datetime(start_date=start_date, tzinfo=timezone.utc) for _ in range(size)])


def generate_username(size=1):
    return [fake.user_name() for _ in range(size)]


def get_total_time_for_window(df, get_start_fn=None):
    df = df.sort_values(by=['creation_time'])
    total_time = timedelta(0)
    start_idx = 0
    end_idx = len(df)

    if len(df):
        if df["category"].iloc[0] == "end channel":
            total_time += df["creation_time"].iloc[0] - pd.to_datetime(get_start_fn())
            start_idx = 1

        if df["category"].iloc[-1] == "start channel":
            total_time += pd.to_datetime(get_time()) - df["creation_time"].iloc[-1]
            end_idx -= 1

    df = df.iloc[start_idx: end_idx]
    enter_df = df[df["category"] == "start channel"]["creation_time"]
    exit_df = df[df["category"] == "end channel"]["creation_time"]
    total_time += pd.to_timedelta((exit_df.values - enter_df.values).sum())
    total_time = timedelta_to_hours(total_time)

    if total_time < 0:
        raise Exception("study time below zero")

    return total_time


def get_redis_client():
    return redis.Redis(
        host=os.getenv("redis_host"),
        port=os.getenv("redis_port"),
        db=int(os.getenv("redis_db_num")),
        username=os.getenv("redis_username"),
        password=os.getenv("redis_password"),
        decode_responses=True
    )


def get_role_status(role_name_to_obj, hours_cur_month):
    cur_role_name = role_names[0]
    next_role_name = role_names[1]

    for role_name, begin_hours in role_name_to_begin_hours.items():
        if begin_hours <= hours_cur_month:
            cur_role_name = role_name
        else:
            next_role_name = role_name
            break

    cur_role = role_name_to_obj[cur_role_name]
    # new members
    if hours_cur_month < role_name_to_begin_hours[cur_role_name]:
        cur_role = None

    next_role, time_to_next_role = (
        role_name_to_obj[next_role_name], round_num(role_name_to_begin_hours[next_role_name] - hours_cur_month)) \
        if cur_role_name != role_names[-1] else (None, None)

    return cur_role, next_role, time_to_next_role


def get_last_line():
    try:
        with open('heartbeat.log', 'rb') as f:
            f.seek(-2, os.SEEK_END)
            while f.read(1) != b'\n':
                f.seek(-2, os.SEEK_CUR)
            line = f.readline().decode()
        return line
    except OSError:
        return None


def get_last_time(line):
    last_line = " ".join(line.split()[:2])
    return datetime.strptime(last_line, "%Y-%m-%d %H:%M:%S.%f")


def kill_last_process(line):
    if not line:
        return

    parts = line.split()
    pid = int(parts[-1].split(":")[-1])

    try:
        process = psutil.Process(pid)

        if "time_counter.py" in " ".join(process.cmdline()):
            process.terminate()
            print(f"{pid} killed")

    except:
        pass


def get_redis_rank(redis_client, sorted_set_name, user_id):
    rank = redis_client.zrevrank(sorted_set_name, user_id)

    if rank is None:
        redis_client.zadd(sorted_set_name, {user_id: 0})
        rank = redis_client.zrevrank(sorted_set_name, user_id)

    return 1 + rank


def get_redis_score(redis_client, sorted_set_name, user_id):
    score = redis_client.zscore(sorted_set_name, user_id) or 0
    return round_num(score)


async def get_user_stats(redis_client, user_id):
    stats = dict()
    category_key_names = get_rank_categories().values()
    for sorted_set_name in list(category_key_names) + ["all_time"]:
        stats[sorted_set_name] = {
            "rank": get_redis_rank(redis_client, sorted_set_name, user_id),
            "study_time": get_redis_score(redis_client, sorted_set_name, user_id)
        }

    return stats


def get_stats_diff(prev_stats, cur_stats):
    prev_studytime = [item["study_time"] for item in prev_stats.values()]
    cur_studytime = [item["study_time"] for item in cur_stats.values()]
    diff = [round_num(cur - prev) for prev, cur in zip(prev_studytime, cur_studytime)]

    return diff


def sleep(seconds):
    # TODO print decimals
    seconds = math.ceil(seconds)

    for remaining in range(seconds, 0, -1):
        sys.stdout.write("\r")
        sys.stdout.write("{:2d} seconds remaining.".format(remaining))
        sys.stdout.flush()
        time.sleep(1)


def increment_studytime(category_key_names, redis_client, user_id, incr=None, last_time=None):
    if incr is None:
        incr = timedelta_to_hours(get_time() - last_time)

    for sorted_set_name in category_key_names:
        redis_client.zincrby(sorted_set_name, incr, user_id)
