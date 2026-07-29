package com.acme.animals;

public abstract class Pet implements Animal {
    private String name;

    public Pet(String name) {
        this.name = name;
    }

    public String getName() {
        return name;
    }

    public abstract String speak();
}
