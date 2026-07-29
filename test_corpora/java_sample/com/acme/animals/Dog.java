package com.acme.animals;

public class Dog extends Pet {
    public Dog(String name) {
        super(name);
    }

    @Override
    public String speak() {
        return getName() + " says Woof";
    }
}
