import marshal

from .dis import tokenize
from .token import Token


class Scanner:

    def tokenize(self, bytecode):
        code_object = marshal.loads(bytecode)
        tokens = tokenize(code_object, Token)
        return tokens
