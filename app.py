from multiprocessing import freeze_support

from finance_app.ui import run


if __name__ in {"__main__", "__mp_main__"}:
    freeze_support()
    run()
