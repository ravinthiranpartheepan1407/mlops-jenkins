import sys
import os
import pytest
import numpy as np

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from src.model import IrisModel


class TestIrisModel:
    def setup_method(self):
        """Setup test fixtures"""
        self.model = IrisModel()
        self.X, self.y = self.model.generate_sample_data()

    def test_model_initialization(self):
        """Test model initializes correctly"""
        assert self.model is not None
        assert not self.model.is_trained

    def test_data_generation(self):
        """Test sample data generation"""
        assert self.X.shape[0] == 1000  # 1000 samples
        assert self.X.shape[1] == 4  # 4 features
        assert len(np.unique(self.y)) == 3  # 3 classes

    def test_model_training(self):
        """Test model training"""
        accuracy = self.model.train(self.X, self.y)

        assert self.model.is_trained
        assert isinstance(accuracy, float)
        assert 0.0 <= accuracy <= 1.0
        assert accuracy > 0.7  # Minimum acceptable accuracy

    def test_model_prediction(self):
        """Test model predictions"""
        # Train model first
        self.model.train(self.X, self.y)

        # Test prediction
        predictions = self.model.predict(self.X[:10])

        assert len(predictions) == 10
        assert all(pred in [0, 1, 2] for pred in predictions)

    def test_prediction_without_training(self):
        """Test that prediction fails without training"""
        with pytest.raises(ValueError):
            self.model.predict(self.X[:5])

    def test_model_save_load(self):
        """Test model saving and loading"""
        # Train and save model
        self.model.train(self.X, self.y)
        model_path = self.model.save_model("models/trained_model.pkl")

        # Create new model instance and load
        new_model = IrisModel()
        new_model.load_model("test_model.pkl")

        # Test loaded model works
        predictions = new_model.predict(self.X[:5])
        assert len(predictions) == 5

        # Cleanup
        if os.path.exists("test_model.pkl"):
            os.remove("test_model.pkl")