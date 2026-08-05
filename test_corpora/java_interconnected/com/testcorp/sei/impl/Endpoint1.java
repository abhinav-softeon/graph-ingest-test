package com.testcorp.sei.impl;

import javax.jws.WebMethod;
import javax.jws.WebService;
import com.testcorp.api.Handler;
import com.testcorp.api.HandlerImpl1;
import com.testcorp.manager.Manager1;
import com.testcorp.facade.Facade1;
import com.testcorp.service.Service21;
import com.testcorp.service.Service1;

@WebService
public class Endpoint1 {

    @WebMethod
    public String lookup(String id) throws Exception {
        return new Service1().handle(id);
    }

    @WebMethod
    public String dispatch(String id) throws Exception {
        Handler h = new HandlerImpl1();
        return h.run(id);
    }

    @WebMethod
    public String viaManager(String id) throws Exception {
        return new Manager1().process(id);
    }

    @WebMethod
    public String deepChain(String id) throws Exception {
        return new Manager1().deep(id);
    }

    @WebMethod
    public String deepest(String id) throws Exception {
        return new Facade1().orchestrate(id);
    }

    @WebMethod
    public String viaFacade(String id) throws Exception {
        return new Facade1().orchestrateDirect(id);
    }

    @WebMethod
    public String lookupAlt1(String id) throws Exception {
        return new Service21().handle(id);
    }
}
