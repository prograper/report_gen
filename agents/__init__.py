# agents/__init__.py
"""
导入所有带注册装饰器的模块，以确保类在 Registry 中注册
"""
# 注册 GenericExtractor
from . import extract_generic
# 注册 GenericParagraphGenerator
from .generate import generic_paragraph 
