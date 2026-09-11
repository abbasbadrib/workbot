# 🤖 ربات مدیریت مالی کارگاه

ربات تلگرامی فارسی برای مدیریت مالی کارگاه نصب و تعمیرات کولرگازی و پکیج.

## ✨ امکانات

- ثبت پروژه با محاسبه خودکار سهم‌ها (حسین ۳۵٪، من ۳۲.۵٪، علی ۳۲.۵٪)
- ثبت هزینه روزانه با قانون شاگرد
- ثبت تسویه بین افراد
- پروژه‌های در انتظار
- گزارش امروز / ماهانه / بدهی‌ها
- دریافت متن و ویس فارسی (Vosk)
- ویرایش، Undo، بکاپ خودکار
- SQLite (بدون ORM)

## 📋 پیش‌نیازها

- Python 3.11+
- ffmpeg

## 🚀 اجرا روی Railway

۱. این مخزن را به Railway متصل کنید
۲. متغیر `BOT_TOKEN` را تنظیم کنید
۳. یک Volume روی `/app/data` بسازید (اختیاری)
۴. Deploy کنید

## ⚙️ متغیرهای محیطی

| متغیر | پیش‌فرض | توضیح |
|-------|---------|-------|
| `BOT_TOKEN` | (اجباری) | توکن ربات تلگرام |
| `DATABASE_PATH` | `/app/data/business.db` | مسیر دیتابیس |
| `VOSK_MODEL_PATH` | `/app/models/vosk-model-small-fa-0.5` | مسیر مدل Vosk |
| `AUTHORIZED_USERS` | (خالی) | شناسه‌های مجاز با کاما |
| `MAX_BACKUPS` | `7` | تعداد بکاپ‌ها |

## 🖥️ اجرای محلی

```bash
# نصب
pip install -r requirements.txt

# دانلود مدل Vosk
wget https://alphacephei.com/vosk/models/vosk-model-small-fa-0.5.zip
unzip vosk-model-small-fa-0.5.zip

# تنظیم توکن
export BOT_TOKEN="توکن_ربات"
export VOSK_MODEL_PATH="vosk-model-small-fa-0.5"

# اجرا
