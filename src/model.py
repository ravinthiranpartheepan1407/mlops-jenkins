import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestClassifier
from sklearn.datasets import make_classification
from sklearn.metrics import accuracy_score
import joblib
import os


class IrisModel:
    def __init__(self):
        self.model = RandomForestClassifier(n_estimators=10, random_state=42)
        self.is_trained = False

    def generate_sample_data(self):
        """Generate sample classification data"""
        X, y = make_classification(
            n_samples=1000,
            n_features=4,
            n_informative=4,  # Fixed: Set n_informative to 4 to support 3 classes
            n_redundant=0,  # Fixed: Set redundant features to 0
            n_classes=3,
            n_clusters_per_class=1,  # Fixed: Reduced clusters per class
            random_state=42
        )
        return X, y

    def train(self, X, y):
        """Train the model"""
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42
        )

        self.model.fit(X_train, y_train)
        self.is_trained = True

        # Calculate accuracy
        y_pred = self.model.predict(X_test)
        accuracy = accuracy_score(y_test, y_pred)

        return accuracy

    def predict(self, X):
        """Make predictions"""
        if not self.is_trained:
            raise ValueError("Model must be trained before making predictions")
        return self.model.predict(X)

    def save_model(self, filepath="models/trained_model.pkl"):
        """Save trained model"""
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        joblib.dump(self.model, filepath)
        return filepath

    def load_model(self, filepath="models/trained_model.pkl"):
        """Load trained model"""
        self.model = joblib.load(filepath)
        self.is_trained = True