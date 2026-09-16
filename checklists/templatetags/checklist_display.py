from django import template

from checklists.presentation import display_task_text


register = template.Library()
register.filter("task_text", display_task_text)
