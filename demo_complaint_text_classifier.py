"""Educational text-classification demo using synthetic complaint examples only.

This module is deliberately separate from the historical staff recommender.
It does not import Flask, application modules, or database libraries, and it
never writes a model artifact.
"""

import json


SYNTHETIC_EXAMPLES = (
    ("A fictional pipe leak is flooding the sample street.", "Water Supply"),
    ("Synthetic residents report a broken water main in the demo block.", "Water Supply"),
    ("Example notice: clean water is not reaching the practice neighborhood.", "Water Supply"),
    ("A made-up service request describes low pressure from the demo tap.", "Water Supply"),
    ("Demo report of a leaking supply pipe near the sample park.", "Water Supply"),
    ("Fictional water service interruption affects the example building.", "Water Supply"),
    ("A fictional road has a pothole in this sample scenario.", "Road Maintenance"),
    ("Synthetic report of cracked pavement on the demo avenue.", "Road Maintenance"),
    ("Example request: repair the damaged sample roadway.", "Road Maintenance"),
    ("A made-up street surface is uneven beside the practice school.", "Road Maintenance"),
    ("Demo notice of a broken curb and pavement on a fictional road.", "Road Maintenance"),
    ("Fictional traffic lane has a surface crack in this example.", "Road Maintenance"),
    ("A fictional collection point has overflowing sample rubbish bins.", "Waste Management"),
    ("Synthetic report says demo-block garbage was not collected.", "Waste Management"),
    ("Example request to remove litter from the practice neighborhood.", "Waste Management"),
    ("A made-up street has waste bags waiting for sample pickup.", "Waste Management"),
    ("Demo report of an overflowing refuse container near the example park.", "Waste Management"),
    ("Fictional household waste collection was missed in this scenario.", "Waste Management"),
)

DEMO_PROMPTS = (
    "Synthetic example: a water pipe is leaking on the demo street.",
    "Synthetic example: repair a pothole on the fictional avenue.",
    "Synthetic example: sample garbage collection was missed.",
)


def run_demo():
    """Fit and evaluate a temporary classifier on fixed synthetic examples."""
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import accuracy_score, f1_score
        from sklearn.model_selection import train_test_split
        from sklearn.pipeline import Pipeline
    except ImportError as error:
        raise RuntimeError("The text demo requires the installed scikit-learn dependency.") from error

    texts = [text for text, _label in SYNTHETIC_EXAMPLES]
    labels = [label for _text, label in SYNTHETIC_EXAMPLES]
    train_texts, test_texts, train_labels, test_labels = train_test_split(
        texts,
        labels,
        test_size=1 / 3,
        random_state=23,
        stratify=labels,
    )
    classifier = Pipeline([
        ("tfidf", TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True)),
        ("classifier", LogisticRegression(max_iter=1000, random_state=23)),
    ])
    classifier.fit(train_texts, train_labels)
    predictions = classifier.predict(test_texts)
    class_labels = sorted(set(labels))

    return {
        "demo_status": "completed",
        "dataset_type": "fixed synthetic demonstration examples",
        "purpose": "illustrate text classification only",
        "staff_recommendation_model": False,
        "production_routing_changed": False,
        "artifact_written": False,
        "split": "stratified fixed-seed holdout; synthetic examples only",
        "training_example_count": len(train_texts),
        "evaluation_example_count": len(test_texts),
        "synthetic_holdout_metrics": {
            "accuracy": float(accuracy_score(test_labels, predictions)),
            "macro_f1": float(f1_score(
                test_labels, predictions, labels=class_labels, average="macro", zero_division=0,
            )),
        },
        "synthetic_example_predictions": [
            {"example_number": index + 1, "predicted_department": str(label)}
            for index, label in enumerate(classifier.predict(DEMO_PROMPTS))
        ],
        "limitations": [
            "These fixed examples are synthetic and do not measure real-world performance.",
            "This demo does not use assignment history or recommend staff.",
            "The result does not replace or modify the application's routing rules.",
        ],
    }


def main():
    try:
        result = run_demo()
    except RuntimeError as error:
        print(json.dumps({"demo_status": "unavailable", "reason": str(error)}))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
