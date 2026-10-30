Добавьте эти строки в существующий корневой .gitignore. Не заменяйте весь файл:

# MAX bot runtime configuration and private state
max_bot/.env
max_bot/data/
max_bot/*.sqlite3
max_bot/*.sqlite3-*
__pycache__/
*.py[cod]
