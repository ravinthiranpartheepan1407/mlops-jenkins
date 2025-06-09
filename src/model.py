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
            n_classes=3,
            n_informative=3,  # Fixed: increased from 2 to 3
            n_redundant=1,  # Added: to make total features = 4
            n_clusters_per_class=1,  # Fixed: reduced from 2 to 1
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
        # Only create directory if filepath contains a directory
        dir_path = os.path.dirname(filepath)
        if dir_path:  # Only create if there's actually a directory path
            os.makedirs(dir_path, exist_ok=True)
        joblib.dump(self.model, filepath)
        return filepath

    def load_model(self, filepath="models/trained_model.pkl"):
        """Load trained model"""
        self.model = joblib.load(filepath)
        self.is_trained = True