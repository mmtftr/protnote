import logging
from src.utils.data import load_gz_json
from src.utils.evaluation import EvalMetrics
from src.utils.losses import BatchWeightedBCE, FocalLoss, RGDBCE
from torchmetrics import MetricCollection, Metric
from src.utils.proteinfer import normalize_confidences
import numpy as np
import torch
import wandb
import os
import json
from collections import defaultdict
import torch.cuda.nvtx as nvtx

class ProTCLTrainer:
    def __init__(
        self,
        model: torch.nn.Module,
        device: str,
        config: dict,
        vocabularies: dict,
        logger: logging.Logger,
        timestamp: str,
        run_name: str,
        use_wandb: bool = False,
        bce_pos_weight: torch.Tensor = None,

    ):
        """_summary_
        :param model: pytorch model
        :type model: torch.nn.Module
        :param device: decide for training on cpu or gpu
        :type device: str
        :param config: Training configuration
        :type config: dict
        :param logger: logger
        :type logger: logging.Logger
        :param timestamp: run timestamp
        :type timestamp: str
        :param use_wandb: whether to use weights and biases, defaults to False
        :type use_wandb: bool, optional
        :param run_name: name of the run
        :type run_name: str
        """

        self.model = model
        self.device = device
        self.run_name = run_name
        self.logger = logger
        self.timestamp = timestamp
        self.use_wandb = use_wandb
        self.num_epochs = config["params"]["NUM_EPOCHS"]
        self.train_sequence_encoder = config["params"]["TRAIN_SEQUENCE_ENCODER"]
        self.train_label_encoder = config["params"]["TRAIN_LABEL_ENCODER"]
        self.train_projection_head = config["params"]["TRAIN_PROJECTION_HEAD"]

        self.normalize_probabilities = config["params"]["NORMALIZE_PROBABILITIES"]
        self.validations_per_epoch = config["params"]["VALIDATIONS_PER_EPOCH"]
        self.gradient_accumulation_steps = config["params"]["GRADIENT_ACCUMULATION_STEPS"]
        self.vocabularies = vocabularies
        self.label_normalizer = load_gz_json(
            config["paths"]["PARENTHOOD_LIB_PATH"]
        )
        self.output_model_dir = config["paths"]["OUTPUT_MODEL_DIR"]
        self._set_optimizer(config["params"]["LEARNING_RATE"])
        self.bce_pos_weight = bce_pos_weight
        self.loss_fn = self._get_loss_fn(config)
        self.model_path = self._get_saved_model_path()
        self.best_val_metric = 0.0

    def _get_saved_model_path(self):
            # Save model to OUTPUT_MODEL_DIR. Create path if it doesn't exist.
        if not os.path.exists(self.output_model_dir):
            os.makedirs(self.output_model_dir)

        model_name = (
            self.run_name if self.run_name else "best_ProTCL.pt"
        )
        model_path = os.path.join(
            self.output_model_dir, f"{self.timestamp}_{model_name}.pt"
        )
        return model_path

    # TODO: Eventually use factory method to get loss_fn based on config
    def _get_loss_fn(self, config):
        if config["params"]["LOSS_FN"] == "BCE":
            assert self.bce_pos_weight is not None, "bce_pos_weight must be provided for BCE loss"
            return torch.nn.BCEWithLogitsLoss(reduction='mean', pos_weight=self.bce_pos_weight)
        elif config["params"]["LOSS_FN"] == "BatchWeightedBCE":
            return BatchWeightedBCE()
        elif config["params"]["LOSS_FN"] == "FocalLoss":
            assert (config["params"]["FOCAL_LOSS_GAMMA"] is not None)\
                   &(config["params"]["FOCAL_LOSS_ALPHA"] is not None), "gamma and gamma must be provided for FocalLoss"
            return FocalLoss(gamma=config["params"]["FOCAL_LOSS_GAMMA"], alpha=config["params"]["FOCAL_LOSS_ALPHA"])
        elif config["params"]["LOSS_FN"]=="RGDBCE":
            return RGDBCE()
        else:
            raise ValueError(f"Unknown loss function {config['params']['LOSS_FN']}")


    def to_device(self, *args):
        return [item.to(self.device) if item is not None else None for item in args]

    def _set_optimizer(self, lr):
        trainable_params = []
        trainable_params_names = []

        for name, param in self.model.named_parameters():
            if name.startswith('sequence_encoder') and (not self.train_sequence_encoder):
                param.requires_grad = False

            if name.startswith('label_encoder') and (not self.train_label_encoder):
                param.requires_grad = False

            if (name.startswith('W_p.weight') or name.startswith('W_l.weight')) and (not self.train_projection_head):
                param.requires_grad = False

            if param.requires_grad:
                trainable_params.append(param)
                trainable_params_names.append(name)

        self.trainable_params_names = trainable_params_names

        self.optimizer = torch.optim.Adam(
            trainable_params, lr=lr
        )

    def evaluation_step(self, batch) -> tuple:
        """Perform a single evaluation step.

        :param batch: _description_
        :type batch: _type_
        :return: batch loss, logits and labels
        :rtype: tuple
        """

        # Unpack the validation or testing batch
        sequence_onehots, sequence_embeddings, sequence_lengths, sequence_ids, label_multihots, tokenized_labels, label_embeddings = (
            batch["sequence_onehots"],
            batch["sequence_embeddings"],
            batch["sequence_lengths"],
            batch["sequence_ids"],
            batch["label_multihots"],
            batch["tokenized_labels"],
            batch["label_embeddings"]
        )

        # Move all unpacked batch elements to GPU, if available
        sequence_onehots, sequence_embeddings, sequence_lengths, label_multihots, tokenized_labels, label_embeddings = self.to_device(
            sequence_onehots, sequence_embeddings, sequence_lengths, label_multihots, tokenized_labels, label_embeddings)

       # Forward pass
        inputs = {
            "sequence_onehots": sequence_onehots,
            "sequence_embeddings": sequence_embeddings,
            "sequence_lengths": sequence_lengths,
            "tokenized_labels": tokenized_labels,
            "label_embeddings": label_embeddings
        }
        logits = self.model(**inputs)

        # Compute validation loss for the batch
        loss = self.loss_fn(logits, label_multihots.float())

        return loss.item(), logits, label_multihots, sequence_ids

    def validate(self, 
                 val_loader: torch.utils.data.DataLoader,
                 val_optimization_metric: Metric,
                 val_optimization_metric_name: str
                 ):

        self.logger.info("Running validation...")
    
        val_metrics, _ = self.evaluate(data_loader=val_loader,
                                       eval_metrics=MetricCollection({val_optimization_metric_name: val_optimization_metric}))

        self.logger.info(val_metrics)

        if self.use_wandb:
            wandb.log({f'validation_{k}':v for k,v in val_metrics.items()})

        # Save the model if it has the best validation loss so far
        if val_metrics[val_optimization_metric_name] > self.best_val_metric:
            self.logger.info(
                f"New best {val_optimization_metric_name}: {val_metrics[val_optimization_metric_name]}. Saving model..."
            )
            self.best_val_metric = val_metrics[val_optimization_metric_name]

            torch.save(self.model.state_dict(), self.model_path)
            self.logger.info(f"Saved model to {self.model_path}.")

            if self.use_wandb:
                wandb.save(f"{self.timestamp}_best_ProTCL.pt")

        return val_metrics

    def find_optimal_threshold(
        self, data_loader: torch.utils.data.DataLoader, optimization_metric_name: str
    ) -> tuple[float, float]:
        """Find the optimal threshold for the given data loader.

        :param data_loader: _description_
        :type data_loader: torch.utils.data.DataLoader
        :param average: _description_
        :type average: Literal[&#39;micro&#39;, &#39;macro&#39;, &#39;weighted&#39;]
        :param optimization_metric_name: _description_
        :type optimization_metric_name: str
        :return: _description_
        :rtype: tuple[float, float]
        """

        self.logger.info("Finding optimal threshold...")
        self.model.eval()

        best_th = 0.0
        best_score = 0.0

        with torch.no_grad():
            all_probabilities = []
            all_label_multihots = []
            for batch in data_loader:
                _, logits, label_multihots,_ = self.evaluation_step(
                    batch=batch)

                # Apply sigmoid to get the probabilities for multi-label classification
                probabilities = torch.sigmoid(logits)

                if self.normalize_probabilities:
                    # TODO: Using original normalize_confidences implemented with numpy,
                    # but this is slow. Should be able to do this with torch tensors.
                    probabilities = torch.tensor(
                        normalize_confidences(
                            predictions=probabilities.detach().cpu().numpy(),
                            label_vocab=self.vocabularies["GO_label_vocab"],
                            applicable_label_dict=self.label_normalizer,
                        ),
                        device=self.device,
                    )

                all_probabilities.append(probabilities)
                all_label_multihots.append(label_multihots)

            all_probabilities = torch.cat(all_probabilities)
            all_label_multihots = torch.cat(all_label_multihots)

        for th in np.arange(0.1, 1, 0.01):
            optimization_metric = EvalMetrics(device=self.device)\
                .get_metric_by_name(name = optimization_metric_name,
                                    threshold=th,
                                    num_labels=label_multihots.shape[-1])

            optimization_metric(all_probabilities, all_label_multihots)
            score = optimization_metric.compute().item()
            if score > best_score:
                best_score = score
                best_th = th
            print("TH:", th, "F1:", score)

        best_score = best_score
        self.logger.info(
            f"Best validation score: {best_score}, Best val threshold: {best_th}"
        )
        self.model.train()
        return best_th, best_score

    def evaluate(
        self,
        data_loader: torch.utils.data.DataLoader,
        eval_metrics: MetricCollection = None,
        save_results: bool = False
    ) -> tuple[dict, dict]:
        """Evaluate the model on the given data loader.
        :param data_loader: pytorch data loader
        :type data_loader: torch.utils.data.DataLoader
        :param eval_metrics: an eval metrics class to calculate metrics like F1 score, defaults to None
        :type eval_metrics: EvalMetrics, optional
        :return: dictionary with evaluation metrics. Always return avg_loss and if eval_metrics is not None, it will return the metrics from eval_metrics.compute()
        :rtype: dict
        """
        self.model.eval()
        total_loss = 0
        test_results = defaultdict(list)
        with torch.no_grad():
            for batch in data_loader:
                loss, logits, labels, sequence_ids = self.evaluation_step(
                    batch=batch)
                if eval_metrics is not None:
                    # Apply sigmoid to get the probabilities for multi-label classification
                    probabilities = torch.sigmoid(logits)

                    if self.normalize_probabilities:
                        # TODO: Using original normalize_confidences implemented with numpy,
                        # but this is slow. Should be able to do this with torch tensors.
                        probabilities = torch.tensor(
                            normalize_confidences(
                                predictions=probabilities.detach().cpu().numpy(),
                                label_vocab=self.vocabularies["GO_label_vocab"],
                                applicable_label_dict=self.label_normalizer,
                            ),
                            device=self.device,
                        )
                    # Update eval metrics
                    eval_metrics(probabilities, labels)

                    #No need to save results everytime. Only need it for final evaluation.
                    if save_results:
                        test_results["sequence_ids"].append(sequence_ids)
                        test_results["probabilities"].append(probabilities)
                        test_results["labels"].append(labels)

                # Accumulate loss
                total_loss += loss
            
            if save_results:
                for key in test_results.keys():
                    if key == "sequence_ids":
                        test_results[key] = (
                            np.array([[j] for i in test_results["sequence_ids"] for j in i])
                        )
                    else:
                        test_results[key] = (
                            torch.cat(test_results[key]).detach().cpu().numpy()
                        )

            # Compute average validation loss
            avg_loss = total_loss / len(data_loader)
            final_metrics = eval_metrics.compute() if eval_metrics is not None else {}

            for k, v in final_metrics.items():
                if isinstance(v, torch.Tensor):
                    final_metrics[k] = v.item()
                
                #Cast numpy floats to float32. Needed to store as parquet
                # because pyarrow doesn't support float16 from mixed precision
                if np.issubdtype(type(v), np.floating):
                    final_metrics[k] = v.astype("float32")

            final_metrics.update({"avg_loss": avg_loss})
            
        self.model.train()
        return final_metrics, test_results

    def train(
        self,
        train_loader: torch.utils.data.DataLoader,
        val_loader: torch.utils.data.DataLoader,
        val_optimization_metric: Metric,
        val_optimization_metric_name: str
    ):
        """Train model
        :param train_loader: _description_
        :type train_loader: torch.utils.data.DataLoader
        :param val_loader: _description_
        :type val_loader: torch.utils.data.DataLoader
        """
        # Log that training is starting
        self.logger.info("Starting training...")

        self.model.train()
        # Watch the model
        if self.use_wandb:
            wandb.watch(self.model)

        # Compute total number of training steps
        num_training_steps = len(train_loader) * self.num_epochs
        self.logger.info(
            f"Total number of training steps: {num_training_steps}")
        batch_count = 0

        for epoch in range(self.num_epochs):
            ####### TRAINING LOOP #######
            with torch.autograd.profiler.emit_nvtx():
                nvtx.range_push("Data loading")  # Profiling: Data loading
                for batch in train_loader:
                    # Increment batch index
                    batch_count += 1
                    nvtx.range_pop()  # Profiling: End data loading

                    # Unpack the training batch
                    sequence_onehots, sequence_embeddings, sequence_lengths, label_multihots, tokenized_labels, label_embeddings = (
                        batch["sequence_onehots"],
                        batch["sequence_embeddings"],
                        batch["sequence_lengths"],
                        batch["label_multihots"],
                        batch["tokenized_labels"],
                        batch["label_embeddings"]
                    )

                    if batch_count == 100:
                        torch.cuda.cudart().cudaProfilerStart()  # Profiling: Start profiling
                    # Profiling: Batch
                    nvtx.range_push("Batch" + str(batch_count))

                    # Profiling: Copy to device
                    nvtx.range_push("Copy to device")
                    # Move all unpacked batch elements to GPU, if available
                    sequence_onehots, sequence_embeddings, sequence_lengths, label_multihots, tokenized_labels, label_embeddings = self.to_device(
                        sequence_onehots, sequence_embeddings, sequence_lengths, label_multihots, tokenized_labels, label_embeddings)

                    nvtx.range_pop()  # Profiling: End copy to device

                    nvtx.range_push("Forward pass")  # Profiling: Forward pass
                    # Forward pass
                    inputs = {
                        "sequence_onehots": sequence_onehots,
                        "sequence_embeddings": sequence_embeddings,
                        "sequence_lengths": sequence_lengths,
                        "tokenized_labels": tokenized_labels,
                        "label_embeddings": label_embeddings
                    }
                    logits = self.model(**inputs)


                    # log average probabilities to W&B
                    if self.use_wandb:
                        with torch.no_grad():
                            avg_probabilities = torch.mean(torch.sigmoid(logits))
                            avg_grounth_truth =  torch.mean(label_multihots.float())
                            wandb.log({"avg_probabilities":avg_probabilities ,
                                        "avg_grounth_truth":avg_grounth_truth,
                                        "avg_probabilities_ground_truth_ratio":avg_probabilities/avg_grounth_truth})


                    # Compute loss
                    loss = self.loss_fn(logits, label_multihots.float()) / \
                        self.gradient_accumulation_steps

                    # Log metrics to W&B
                    if self.use_wandb:
                        wandb.log({"train_loss": loss.item()})
                    nvtx.range_pop()  # Profiling: End forward pass

                    # Profiling: Backward pass
                    nvtx.range_push("Backward pass")
                    # Backward pass
                    loss.backward()

                    # Gradient accumulation every GRADIENT_ACCUMULATION_STEPS
                    if (batch_count % self.gradient_accumulation_steps == 0):
                        self.optimizer.step()
                        self.optimizer.zero_grad()

                    nvtx.range_pop()  # Profiling: End backward pass

                    nvtx.range_pop()  # Profiling: End batch

                    # Log training progress percentage every 2%
                    if num_training_steps > 50 and batch_count % int(num_training_steps/50) == 0:
                        self.logger.info(
                            f"Training progress: {round(100*batch_count/num_training_steps,2)}%")

                    if batch_count == 150:
                        torch.cuda.cudart().cudaProfilerStop()  # Profiling: Stop profiling

                    # Run validation and log progress every n batches
                    if batch_count != 0 and batch_count % (len(train_loader) // self.validations_per_epoch) == 0:
                        ####### VALIDATION LOOP #######
                        # Force model to recompute all label embeddings
                        if self.train_label_encoder:
                            self.model.clear_label_embeddings_cache()

                        # Run validation
                        val_metrics = self.validate(val_loader=val_loader,
                                                    val_optimization_metric=val_optimization_metric,
                                                    val_optimization_metric_name=val_optimization_metric_name
                                                    )

                        self.logger.info(
                            f"Epoch {epoch+1}/{self.num_epochs}, Batch {batch_count}, Training Loss: {loss.item()}"
                        )

                        self.logger.info(
                            f"Validation metrics:\n{json.dumps(val_metrics, indent=4)}")

                    nvtx.range_push("Data loading")  # Profiling: Data loading
                nvtx.range_pop()  # Profiling: End data loading

        #Set weights to best model
        self.model.load_state_dict(torch.load(self.model_path))
        self.logger.info("Restoring model to best validation loss...")
