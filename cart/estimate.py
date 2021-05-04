__all__ = ["AccumulateCartStatisticsJob", "EstimateCartJob"]

import os
import shutil

from sisyphus import *

Path = setup_path(__package__)

import recipe.i6_core.rasr as rasr
import recipe.i6_core.util as util


class AccumulateCartStatisticsJob(rasr.RasrCommand, Job):
    def __init__(
        self,
        crp,
        alignment_flow,
        keep_accumulators=False,
        extra_config_accumulate=None,
        extra_post_config_accumulate=None,
        extra_config_merge=None,
        extra_post_config_merge=None,
    ):
        self.set_vis_name("Accumulate CART")

        kwargs = locals()
        del kwargs["self"]

        (
            self.config_accumulate,
            self.post_config_accumulate,
        ) = AccumulateCartStatisticsJob.create_accumulate_config(**kwargs)
        (
            self.config_merge,
            self.post_config_merge,
        ) = AccumulateCartStatisticsJob.create_merge_config(**kwargs)
        self.alignment_flow = alignment_flow
        self.exe = self.select_exe(
            crp.acoustic_model_trainer_exe, "acoustic-model-trainer"
        )
        self.keep_accumulators = keep_accumulators
        self.concurrent = crp.concurrent

        self.accumulate_log_file = self.log_file_output_path("accumulate", crp, True)
        self.merge_log_file = self.log_file_output_path("merge", crp, False)
        self.cart_sum = self.output_path("cart.sum.xml.gz", cached=True)

        self.rqmt = {
            "time": max(crp.corpus_duration / (20 * crp.concurrent), 0.5),
            "cpu": 1,
            "mem": 4,
        }

    def tasks(self):
        yield Task("create_files", mini_task=True)
        yield Task(
            "accumulate",
            resume="accumulate",
            rqmt=self.rqmt,
            args=range(1, self.concurrent + 1),
        )
        yield Task("merge", mini_task=True)

    def create_files(self):
        self.write_config(
            self.config_accumulate, self.post_config_accumulate, "accumulate.config"
        )
        self.write_config(self.config_merge, self.post_config_merge, "merge.config")
        self.alignment_flow.write_to_file("alignment.flow")
        self.write_run_script(self.exe, "accumulate.config", "accumulate.sh")
        self.write_run_script(self.exe, "merge.config", "merge.sh")

    def accumulate(self, task_id):
        self.run_script(task_id, self.accumulate_log_file[task_id], "./accumulate.sh")

    def merge(self):
        self.run_script(1, self.merge_log_file, "./merge.sh")
        shutil.move("cart.sum.xml.gz", self.cart_sum.get_path())
        if not self.keep_accumulators:
            for i in range(1, self.concurrent + 1):
                os.remove("cart.acc.xml.%d.gz" % i)

    def cleanup_before_run(self, cmd, retry, task_id, *args):
        if cmd == "./accumulate.sh":
            util.backup_if_exists("accumulate.log.%d" % task_id)
        elif cmd == "./merge.sh":
            util.backup_if_exists("merge.log")

    @classmethod
    def create_accumulate_config(
        cls,
        crp,
        alignment_flow,
        extra_config_accumulate,
        extra_post_config_accumulate,
        **kwargs,
    ):
        config, post_config = rasr.build_config_from_mapping(
            crp,
            {
                "corpus": "acoustic-model-trainer.corpus",
                "lexicon": "acoustic-model-trainer.cart-trainer.lexicon",
                "acoustic_model": "acoustic-model-trainer.cart-trainer.acoustic-model",
            },
            parallelize=True,
        )

        config.acoustic_model_trainer.action = "accumulate-cart-examples"
        config.acoustic_model_trainer.aligning_feature_extractor.feature_extraction.file = (
            "alignment.flow"
        )
        config.acoustic_model_trainer.cart_trainer.example_file = (
            "`cf -d cart.acc.xml.$(TASK).gz`"
        )

        alignment_flow.apply_config(
            "acoustic-model-trainer.aligning-feature-extractor.feature-extraction",
            config,
            post_config,
        )

        config._update(extra_config_accumulate)
        post_config._update(extra_post_config_accumulate)

        return config, post_config

    @classmethod
    def create_merge_config(
        cls, crp, extra_config_merge, extra_post_config_merge, **kwargs
    ):
        config, post_config = rasr.build_config_from_mapping(crp, {})

        config.acoustic_model_trainer.action = "merge-cart-examples"
        config.acoustic_model_trainer.cart_trainer.merge_example_files = " ".join(
            "cart.acc.xml.%d.gz" % i for i in range(1, crp.concurrent + 1)
        )
        config.acoustic_model_trainer.cart_trainer.example_file = (
            "`cf -d cart.sum.xml.gz`"
        )

        config._update(extra_config_merge)
        post_config._update(extra_post_config_merge)

        return config, post_config

    @classmethod
    def hash(cls, kwargs):
        config_acc, post_config_acc = cls.create_accumulate_config(**kwargs)
        config_merge, post_config_merge = cls.create_merge_config(**kwargs)
        return super().hash(
            {
                "config_accumulate": config_acc,
                "config_merge": config_merge,
                "alignment_flow": kwargs["alignment_flow"],
                "exe": kwargs["crp"].acoustic_model_trainer_exe,
            }
        )


class EstimateCartJob(rasr.RasrCommand, Job):
    def __init__(
        self,
        crp,
        questions,
        cart_examples,
        variance_clipping=5e-6,
        generate_cluster_file=False,
        extra_config=None,
        extra_post_config=None,
    ):
        self.set_vis_name("Estimate CART")

        kwargs = locals()
        del kwargs["self"]

        self.config, self.post_config = EstimateCartJob.create_config(**kwargs)
        self.exe = self.select_exe(
            crp.acoustic_model_trainer_exe, "acoustic-model-trainer"
        )
        self.questions = questions
        self.generate_cluster_file = generate_cluster_file
        self.concurrent = crp.concurrent

        self.log_file = self.log_file_output_path("cart", crp, False)
        self.cart_tree = self.output_path("cart.tree.xml.gz")
        if generate_cluster_file:
            self.cart_cluster = self.output_path("cart.cluster.xml.gz")
        self.num_labels = self.output_var("num_labels", pickle=False)

        self.rqmt = {"time": 0.5, "cpu": 1, "mem": 1}

    def tasks(self):
        yield Task("create_files", mini_task=True)
        yield Task("run", resume="run", rqmt=self.rqmt)

    def create_files(self):
        self.write_config(self.config, self.post_config, "cart.config")
        if not type(self.questions) == str and not tk.is_path(self.questions):
            self.questions.write_to_file("questions.xml")
        self.write_run_script(self.exe, "cart.config")

    def run(self):
        self.run_script(1, self.log_file)
        shutil.move("cart.tree.xml.gz", self.cart_tree.get_path())
        if self.generate_cluster_file:
            shutil.move("cart.cluster.xml.gz", self.cart_cluster.get_path())
        self.num_labels.set(util.num_cart_labels(self.cart_tree.get_path()))

    def cleanup_before_run(self, *args):
        util.backup_if_exists("cart.log")

    @classmethod
    def create_config(
        cls,
        crp,
        questions,
        cart_examples,
        variance_clipping,
        generate_cluster_file,
        extra_config,
        extra_post_config,
        **kwargs,
    ):
        if not type(questions) == str and not tk.is_path(questions):
            questions_path = "questions.xml"
        else:
            questions_path = questions

        config, post_config = rasr.build_config_from_mapping(crp, {}, parallelize=False)

        config.acoustic_model_trainer.action = "estimate-cart"
        config.acoustic_model_trainer.cart_trainer.training_file = questions_path
        config.acoustic_model_trainer.cart_trainer.example_file = cart_examples
        config.acoustic_model_trainer.cart_trainer.decision_tree_file = (
            "`cf -d cart.tree.xml.gz`"
        )
        if generate_cluster_file:
            config.acoustic_model_trainer.cart_trainer.cluster_file = (
                "`cf -d cart.cluster.xml.gz`"
            )
        config.acoustic_model_trainer.cart_trainer.log_likelihood_gain.variance_clipping = (
            variance_clipping
        )

        config._update(extra_config)
        post_config._update(extra_post_config)

        return config, post_config

    @classmethod
    def hash(cls, kwargs):
        config, post_config = cls.create_config(**kwargs)
        questions = kwargs["questions"]
        return super().hash(
            {
                "config": config,
                "exe": kwargs["crp"].acoustic_model_trainer_exe,
                "questions": questions,
            }
        )
