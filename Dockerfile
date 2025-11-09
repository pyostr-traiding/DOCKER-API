FROM python:3.14

# Установка переменных окружения
ENV PYTHONUNBUFFERED 1

# Установка Poetry
RUN pip install poetry

# Создание рабочей директории
RUN mkdir /code
RUN mkdir /code/logs
WORKDIR /code

# Копирование файлов проекта
COPY pyproject.toml /code/
COPY . /code/

# Установка зависимостей
RUN poetry config virtualenvs.create false \
    && poetry install --no-interaction --no-ansi --no-root

# Запускаем сервер
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
