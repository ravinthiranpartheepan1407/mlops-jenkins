import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from src.model import IrisModel


def main():
    print("Starting model training...")

    # Initialize model
    model = IrisModel()

    # Generate sample data
    X, y = model.generate_sample_data()
    print(f"Generated dataset with {X.shape[0]} samples and {X.shape[1]} features")

    # Train model
    accuracy = model.train(X, y)
    print(f"Model trained successfully! Accuracy: {accuracy:.4f}")

    # Save model
    model_path = model.save_model()
    print(f"Model saved to: {model_path}")

    if accuracy > 0.8:
        print("[SUCCESS] Training completed successfully - Model meets accuracy threshold")
        return 0
    else:
        print("[FAILED] Training failed - Model accuracy below threshold")
        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)