from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn, TimeRemainingColumn


def bar():
    return Progress(TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(),
                    TimeElapsedColumn(), TextColumn("剩余"), TimeRemainingColumn(),
                    redirect_stdout=False, redirect_stderr=False)
