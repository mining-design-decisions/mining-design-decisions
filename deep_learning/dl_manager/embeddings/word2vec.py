import pathlib

from gensim.models import Word2Vec as GensimWord2Vec

from ..config import Argument, IntArgument
from .embedding_generator import AbstractEmbeddingGenerator



class Word2VecGenerator(AbstractEmbeddingGenerator):

    def generate_embedding(self, issues: list[str], path: pathlib.Path):
        min_count = self.params['min-count']
        vector_size = self.params['vector-size']
        model = GensimWord2Vec(issues, min_count=min_count, vector_size=vector_size)
        model.wv.save_word2vec_format(path, binary=True)

    @staticmethod
    def get_arguments() -> dict[str, Argument]:
        return super(Word2VecGenerator, Word2VecGenerator).get_arguments() | {
            'vector-size': IntArgument(
                name='vector-size',
                description='Size of the vectors generated by Doc2Vec',
                minimum=2,
                maximum=10000
            ),
            'min-count': IntArgument(
                name='min-count',
                description='Minimum amount of occurrences for a word to be included',
                minimum=0,
                maximum=10000
            )
        }
